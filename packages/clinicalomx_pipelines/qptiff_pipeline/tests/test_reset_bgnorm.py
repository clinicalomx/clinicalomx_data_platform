"""reset-bgnorm, against an in-process lamindb stand-in that logs every mutation.

Checks that ``dry_run`` touches nothing, that only named targets are reset, that an accepted TMA is
skipped, and that the population-wide mode needs its token.
"""
from __future__ import annotations

import sys
import types

import clinicalomx
import pytest

from conftest import load_flow_module, load_pipeline_module

# Load the real domain modules first: the flow reads feature names and states from them.
states = load_pipeline_module("states")
vocab = load_pipeline_module("vocab")
keys = load_pipeline_module("keys")
reset_bgnorm_mod = load_flow_module("reset_bgnorm")
# The flow under test, without real prefect's retry wrapper.
reset_bgnorm = reset_bgnorm_mod.reset_bgnorm
if hasattr(reset_bgnorm, "with_options"):
    reset_bgnorm = reset_bgnorm.with_options(retry_delay_seconds=0)

S = states.S


# The stand-in.


class FakeArtifact:
    """The Artifact surface this flow uses: identity, key, size, and a recorded delete."""

    _world: World | None = None

    def __init__(self, uid, key, size, world):
        self.uid = uid
        self.key = key
        self.size = size
        self.created_at = uid  # ordered by, never parsed
        self._world = world

    def delete(self, permanent=False, storage=False):
        self._world.deleted.append((self.key, permanent, storage))

    # lamindb's manager surface, enough for `as_artifact` and the folder sweep.
    @classmethod
    def filter(cls, **kw):
        world = World.current
        arts = list(world.artifacts.values())
        if "key" in kw:
            arts = [a for a in arts if a.key == kw["key"]]
        if "key__startswith" in kw:
            arts = [a for a in arts if a.key.startswith(kw["key__startswith"])]
        return _QS(arts)


class _QS(list):
    def first(self):
        return self[0] if self else None

    def order_by(self, _field):
        return self

    def one_or_none(self):
        return self[0] if len(self) == 1 else None


class _Features:
    def __init__(self, record):
        self._record = record

    def get_values(self):
        return dict(self._record.values)

    def remove_values(self, feature):
        self._record.values.pop(feature, None)
        self._record.world.removed.append((self._record.name, feature))


class FakeRecord:
    def __init__(self, uid, name, type_, world, **values):
        self.uid = uid
        self.name = name
        self.type = type_
        self.world = world
        self.values = dict(values)
        self.features = _Features(self)

    @classmethod
    def filter(cls, **kw):
        world = World.current
        recs = list(world.records.values())
        if "type" in kw:
            recs = [r for r in recs if r.type == kw["type"]]
        if "name" in kw:
            recs = [r for r in recs if r.name == kw["name"]]
        if "name__startswith" in kw:
            recs = [r for r in recs if r.name.startswith(kw["name__startswith"])]
        if "uid" in kw:
            recs = [r for r in recs if r.uid == kw["uid"]]
        return _QS(sorted(recs, key=lambda r: r.name))

    @classmethod
    def get(cls, **kw):
        found = cls.filter(**kw)
        if not found:
            raise LookupError(kw)
        return found[0]


class World:
    """The whole fake instance, plus a log of every mutation the flow asked for."""

    current: World | None = None

    def __init__(self):
        self.artifacts: dict[str, FakeArtifact] = {}
        self.records: dict[str, FakeRecord] = {}
        self.states: dict[str, str] = {}
        self.deleted: list[tuple] = []
        self.removed: list[tuple] = []
        self.state_writes: list[tuple] = []
        self.severed: list = []

    # Construction helpers.

    def artifact(self, key, size):
        art = FakeArtifact(f"a{len(self.artifacts):04d}", key, size, self)
        self.artifacts[art.uid] = art
        return art

    def tma(self, name, state, *, n_cores=2, size=1_000_000_000, qptiff="scan.qptiff",
            failed_cores=None, as_key_string=False):
        """A TMA at ``state`` with ``n_cores`` cores, each with a bgnormed zarr and QC parquet."""
        values = {"source_image": qptiff}
        if failed_cores is not None:
            values[vocab.BGNORM_FAILED_CORES] = failed_cores
        tma = FakeRecord(f"t{len(self.records):04d}", name, states.TMA_TYPE, self, **values)
        self.records[tma.uid] = tma
        self.states[tma.uid] = state
        for i in range(n_cores):
            core_name = f"{name}:R0C{i}"
            cdir = keys.core_dir(qptiff, core_name)
            zarr = self.artifact(f"{cdir}/bgnormed-image-run{i}.zarr", size)
            qc = self.artifact(f"{cdir}/bgnorm-qc-run{i}.parquet", 1_000)
            core = FakeRecord(
                f"c{len(self.records):04d}", core_name, states.CORE_TYPE, self,
                source_image=qptiff,
                **{
                    vocab.BGNORMED: zarr.key if as_key_string else zarr,
                    vocab.BGNORM_QC_FEATURE: qc.key if as_key_string else qc,
                    vocab.BGNORM_ATTEMPTS: 1,
                },
            )
            self.records[core.uid] = core
        return tma

    @property
    def untouched(self) -> bool:
        return not (self.deleted or self.removed or self.state_writes or self.severed)


@pytest.fixture
def world(monkeypatch):
    w = World()
    World.current = w

    ln = types.ModuleType("lamindb")
    ln.Artifact = FakeArtifact
    ln.Record = FakeRecord
    ln.finish = lambda: None
    models = types.ModuleType("lamindb.models")

    class _LinkQS:
        db = "default"

        def _raw_delete(self, _db):
            w.severed.append(_db)
            return 0

    class _Objects:
        @staticmethod
        def filter(**_kw):
            return _LinkQS()

    class RecordArtifact:
        objects = _Objects()

    class ArtifactRecord:
        objects = _Objects()

    models.RecordArtifact = RecordArtifact
    models.ArtifactRecord = ArtifactRecord
    ln.models = models

    registry = types.ModuleType("qptiff_pipeline.lamin.registry")
    registry.get_or_create_type = lambda name: name
    tracking = types.ModuleType("qptiff_pipeline.lamin.tracking")
    tracking.track_prefect_run = lambda _name: None

    for name, mod in (
        ("lamindb", ln),
        ("lamindb.models", models),
        ("qptiff_pipeline.lamin.registry", registry),
        ("qptiff_pipeline.lamin.tracking", tracking),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setattr(clinicalomx, "get_state", lambda rec: w.states.get(rec.uid, S.REGISTERED))

    def _set_state(rec, state):
        w.state_writes.append((rec.name, str(state)))
        w.states[rec.uid] = str(state)

    monkeypatch.setattr(clinicalomx, "set_state", _set_state)
    yield w
    World.current = None


# The dry run.


def test_dry_run_touches_nothing_and_reports_the_scale(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=3, size=2_000_000_000)
    out = reset_bgnorm(tma_names=["scan:TMA0"])

    assert world.untouched, (world.deleted, world.removed, world.state_writes)
    assert out["dry_run"] is True
    assert out["cores_reset"] == 3
    assert out["artifacts_deleted"] == 0  # nothing deleted, and the result says so
    assert out["bytes_planned"] == 3 * (2_000_000_000 + 1_000)
    assert out["artifacts_of_unrecorded_size"] == 0
    assert out["tmas_requeued"] == ["scan:TMA0"]  # the plan, reported but not written


def test_dry_run_counts_an_artifact_once_when_the_folder_sweep_also_finds_it(world):
    # The feature value and the folder sweep name the same zarr. Count it once.
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=1, size=5_000_000_000, as_key_string=True)
    out = reset_bgnorm(tma_names=["scan:TMA0"])
    assert out["bytes_planned"] == 5_000_000_000 + 1_000


def test_unrecorded_sizes_are_counted_not_silently_zero(world):
    tma = world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=1, size=1_000_000_000)
    for art in world.artifacts.values():
        art.size = None
    out = reset_bgnorm(tma_uids=[tma.uid])
    assert out["bytes_planned"] == 0
    assert out["artifacts_of_unrecorded_size"] == 2


def test_bytes_of_separates_the_total_from_the_unknowns():
    class _A:
        def __init__(self, size):
            self.size = size

    assert reset_bgnorm_mod._bytes_of([_A(10), _A(None), _A(5)]) == (15, 1)
    assert reset_bgnorm_mod._bytes_of([]) == (0, 0)


# Scoping.


def test_a_named_tma_is_the_only_one_touched(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=2)
    world.tma("scan:TMA1", S.BGNORM_PENDING, n_cores=2)

    out = reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)

    assert out["tmas_reset"] == ["scan:TMA0"]
    assert all("tma0" in key for key, _p, _s in world.deleted), world.deleted
    assert [name for name, _f in world.removed if name.startswith("scan:TMA1")] == []
    assert world.state_writes == [("scan:TMA0", str(S.BGNORM_QUEUED))]


def test_it_still_refuses_a_run_that_names_no_target(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING)
    with pytest.raises(ValueError, match="all_tmas"):
        reset_bgnorm()
    assert world.untouched


def test_it_refuses_all_tmas_alongside_a_named_subset(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING)
    with pytest.raises(ValueError, match="EITHER"):
        reset_bgnorm(tma_names=["scan:TMA0"], all_tmas=True)
    assert world.untouched


def test_all_tmas_plans_freely_but_will_not_apply_without_the_token(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=1)
    world.tma("scan:TMA1", S.BGNORM_PENDING, n_cores=1)

    out = reset_bgnorm(all_tmas=True)
    assert out["tmas_targeted"] == ["scan:TMA0", "scan:TMA1"]
    assert world.untouched

    with pytest.raises(ValueError, match=reset_bgnorm_mod.CONFIRM_ALL):
        reset_bgnorm(all_tmas=True, dry_run=False)
    assert world.untouched

    out = reset_bgnorm(all_tmas=True, dry_run=False, confirm=reset_bgnorm_mod.CONFIRM_ALL)
    assert out["tmas_reset"] == ["scan:TMA0", "scan:TMA1"]
    assert len(world.deleted) == 4


# What is protected.


def test_an_accepted_tma_is_not_silently_reset(world):
    world.tma("scan:TMA0", S.COMPLETE, n_cores=2)
    world.tma("scan:TMA1", S.BGNORM_PENDING, n_cores=2)

    out = reset_bgnorm(all_tmas=True, dry_run=False, confirm=reset_bgnorm_mod.CONFIRM_ALL)

    assert out["skipped_accepted"] == ["scan:TMA0"]
    assert out["tmas_reset"] == ["scan:TMA1"]
    # Skipped whole: bytes, features and state.
    assert all("tma1" in key for key, _p, _s in world.deleted), world.deleted
    assert [n for n, _f in world.removed if n.startswith("scan:TMA0")] == []
    assert ("scan:TMA0", str(S.BGNORM_QUEUED)) not in world.state_writes


def test_include_complete_is_the_only_way_past_that(world):
    world.tma("scan:TMA0", S.COMPLETE, n_cores=1)

    out = reset_bgnorm(
        tma_names=["scan:TMA0"], include_complete=True, dry_run=False,
    )

    assert out["skipped_accepted"] == []
    assert out["tmas_requeued"] == ["scan:TMA0"]
    assert len(world.deleted) == 2


def test_a_tma_parked_upstream_of_bgnorm_is_cleared_but_never_queued(world):
    # Never re-queued from core_pending.
    world.tma("scan:TMA0", S.CORE_PENDING, n_cores=1)

    out = reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)

    assert out["not_requeued"] == ["scan:TMA0"]
    assert out["tmas_requeued"] == []
    assert world.state_writes == []
    assert len(world.deleted) == 2  # its bytes still go


def test_a_running_tma_is_reported_because_its_jobs_may_be_writing(world):
    world.tma("scan:TMA0", S.BGNORM_RUNNING, n_cores=1)
    out = reset_bgnorm(tma_names=["scan:TMA0"])
    assert out["warned_running"] == ["scan:TMA0"]


def test_an_already_queued_tma_is_reset_without_a_redundant_state_write(world):
    world.tma("scan:TMA0", S.BGNORM_QUEUED, n_cores=1)
    out = reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)
    assert out["tmas_requeued"] == []
    assert world.state_writes == []
    assert len(world.deleted) == 2


# Record metadata.


def test_every_core_marker_and_the_tmas_own_result_features_are_cleared(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=1, failed_cores=3)

    reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)

    core_cleared = {f for n, f in world.removed if ":R" in n}
    assert core_cleared == {vocab.BGNORMED, vocab.BGNORM_QC_FEATURE, vocab.BGNORM_ATTEMPTS}
    # Every other runner marker is in the tuple every recovery path clears.
    assert set(vocab.BGNORM_BOOKKEEPING) >= {vocab.BGNORM_ATTEMPTS}
    assert ("scan:TMA0", vocab.BGNORM_FAILED_CORES) in world.removed


def test_a_core_with_no_artifacts_still_has_its_failure_marker_cleared(world):
    tma = world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=1)
    core = next(r for r in world.records.values() if ":R" in r.name)
    core.values = {"source_image": "scan.qptiff", vocab.BGNORM_CORE_FAILED: True}
    world.artifacts.clear()

    out = reset_bgnorm(tma_uids=[tma.uid], dry_run=False)

    assert out["cores_reset"] == 1
    assert (core.name, vocab.BGNORM_CORE_FAILED) in world.removed


# Resumability.


def test_the_core_budget_stops_between_tmas_and_never_splits_one(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=3)
    world.tma("scan:TMA1", S.BGNORM_PENDING, n_cores=3)

    out = reset_bgnorm(all_tmas=True, limit=4, dry_run=False,
                       confirm=reset_bgnorm_mod.CONFIRM_ALL)

    assert out["tmas_reset"] == ["scan:TMA0"]
    assert out["budget_stopped"] is True
    assert out["cores_reset"] == 3  # not 4: TMA1 is left whole for the next run
    assert out["tmas_requeued"] == ["scan:TMA0"]


def test_a_single_tma_larger_than_the_budget_is_still_done_whole(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=5)
    out = reset_bgnorm(all_tmas=True, limit=2, dry_run=False,
                       confirm=reset_bgnorm_mod.CONFIRM_ALL)
    assert out["cores_reset"] == 5
    assert out["budget_stopped"] is False


def test_a_second_run_over_an_already_reset_tma_is_a_no_op_on_bytes(world):
    world.tma("scan:TMA0", S.BGNORM_PENDING, n_cores=2)
    reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)
    world.artifacts.clear()  # the first run deleted them
    world.deleted.clear()

    out = reset_bgnorm(tma_names=["scan:TMA0"], dry_run=False)

    assert world.deleted == []
    assert out["cores_reset"] == 2  # the markers are still swept, so it is idempotent
