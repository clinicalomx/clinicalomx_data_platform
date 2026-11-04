"""Tests for `Snapshot`: bulk reads only, and the narrowing value cache. Nothing connects."""
from __future__ import annotations

import pytest

from clinicalomx import snapshot as snap_mod
from clinicalomx.snapshot import Snapshot


class _Type:
    def __init__(self, name):
        self.name = name


class _Rec:
    """A stand-in ROI Record. Only `uid`, `name` and `type` are read."""

    def __init__(self, uid, name, type_name):
        self.uid, self.name, self.type = uid, name, _Type(type_name)

    def __repr__(self):
        return f"_Rec({self.name!r})"


def _loaded(records: dict[str, list[_Rec]], values: dict[str, dict] | None = None) -> Snapshot:
    """A Snapshot pre-filled as though the bulk reads had run, with a `_values` entry per record."""
    snap = Snapshot()
    snap._roi_types = sorted(records)
    snap._records = dict(records)
    snap._values = {rec.uid: {} for recs in records.values() for rec in recs}
    snap._values.update(values or {})
    snap._values_loaded = sorted(records)
    # Empty, so the fixture never reads ln.Feature.
    snap._artifact_features = ()
    return snap


# Artifact-dtype detection.
# Role features come from declared Vocabularies; provenance features are not roles.
@pytest.mark.parametrize(
    ("dtype", "expected"),
    [("Artifact", True), ("cat[Artifact]", True), ("cat[ Artifact ]", True),
     ("str", False), ("cat[Record]", False), (int, False)],
    ids=["artifact", "cat-artifact", "spaced", "str", "cat-record", "int-class"],
)
def test_artifact_dtype_detection(dtype, expected):
    assert snap_mod._is_artifact_dtype(dtype) is expected


def test_artifact_dtype_detection_matches_the_class_without_importing_lamindb():
    class Artifact:
        pass

    assert snap_mod._is_artifact_dtype(Artifact) is True


def test_role_features_come_from_declared_vocabularies(monkeypatch):
    from clinicalomx.vocab import Vocabulary

    consumer = Vocabulary("some-consumer")
    consumer.feature("detections", type("Artifact", (), {}), "role")
    consumer.feature("bbox_x1", float, "geometry, not a role")
    monkeypatch.setattr("clinicalomx.vocab.registry.discover", lambda: {"c": consumer})

    assert Snapshot().role_features() == ("detections",)


def test_role_features_are_memoised(monkeypatch):
    calls = []

    def _discover():
        calls.append(1)
        return {}

    monkeypatch.setattr("clinicalomx.vocab.registry.discover", _discover)
    snap = Snapshot()
    snap.role_features()
    snap.role_features()
    assert len(calls) == 1


# Absence semantics.
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("a-key", "a-key"),
        (0, 0),                       # falsy but present
        (False, False),
        ({"a", "b"}, {"a", "b"}),
        ([], None),                   # empty multi-value means absent
        (set(), None),
        (None, None),
    ],
    ids=["str", "zero", "false", "set", "empty-list", "empty-set", "none"],
)
def test_present_keeps_falsy_values_and_drops_absent(cell, expected):
    assert snap_mod._present(cell) == expected


def test_present_drops_pandas_nulls():
    pd = pytest.importorskip("pandas")
    assert snap_mod._present(pd.NA) is None
    assert snap_mod._present(float("nan")) is None


def test_present_does_not_raise_on_a_collection():
    pytest.importorskip("pandas")
    assert snap_mod._present(["a", "b"]) == ["a", "b"]


# Bulk value loading.
# Rule 2: one query per subtype, never one per record.
class _Exploding:
    """A record whose per-record read raises, so the bulk path is proven rather than assumed."""

    def __init__(self, uid="u1"):
        self.uid = uid

    @property
    def features(self):
        raise AssertionError("values() fell back to a per-record read")


def test_values_serves_from_the_bulk_load_without_touching_the_record(monkeypatch):
    def _fake_load(self):
        self._values_loaded = ["Core"]
        self._values["u1"] = {"bgnormed": "some/key.zarr"}

    monkeypatch.setattr(Snapshot, "_load_roi_values", _fake_load)
    assert Snapshot().values(_Exploding()) == {"bgnormed": "some/key.zarr"}


def test_values_bulk_loads_once_for_many_records(monkeypatch):
    calls = []

    def _fake_load(self):
        calls.append(1)
        self._values_loaded = ["Core"]
        self._values.update({f"u{i}": {} for i in range(3)})

    monkeypatch.setattr(Snapshot, "_load_roi_values", _fake_load)
    snap = Snapshot()
    for i in range(3):
        snap.values(type("R", (), {"uid": f"u{i}"})())
    assert len(calls) == 1


def test_values_falls_back_for_a_record_outside_the_roi_types(monkeypatch):
    monkeypatch.setattr(Snapshot, "_load_roi_values",
                        lambda self: setattr(self, "_values_loaded", ["Core"]))

    class _Other:
        uid = "other"
        features = type("F", (), {"get_values": staticmethod(lambda: {"x": 1})})()

    assert Snapshot().values(_Other()) == {"x": 1}


def test_load_values_serves_a_non_roi_type_without_per_record_reads(monkeypatch):
    snap = Snapshot()

    def _fake(self, type_name, features=None):
        self._values_loaded = [type_name]
        self._values["c1"] = {"qptiff_ch_index": 0}

    monkeypatch.setattr(Snapshot, "load_values", _fake)
    snap.load_values("QptiffChannels")
    # `_load_roi_values` must not run either.
    monkeypatch.setattr(Snapshot, "_load_roi_values",
                        lambda self: (_ for _ in ()).throw(AssertionError("loaded ROI values")))
    assert snap.values(_Exploding("c1")) == {"qptiff_ch_index": 0}


def test_load_values_is_idempotent_without_even_connecting():
    snap = Snapshot()
    snap._loaded_features["QptiffChannels"] = None   # None: all features loaded
    snap.load_values("QptiffChannels")   # would raise OperationalError if it read
    assert snap._values == {}


def test_a_narrow_load_covers_a_subset_request_without_rereading():
    snap = Snapshot()
    snap._loaded_features["QptiffChannels"] = {"a", "b"}
    snap.load_values("QptiffChannels", features=["a"])
    assert snap._values == {}


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.roi_types(),
        lambda s: s.records("Core"),
        lambda s: s.states("Core"),
        lambda s: s.load_values("Core"),
    ],
    ids=["roi_types", "records", "states", "load_values"],
)
def test_a_cache_hit_does_not_even_connect(call):
    snap = _loaded({"Core": [C1]})
    snap._states["Core"] = {"uC1": "approved"}
    snap._loaded_features["Core"] = None
    call(snap)


@pytest.mark.parametrize(
    ("cached", "asked", "expected"),
    [
        (snap_mod._UNLOADED, None,       None),          # never loaded, want all -> read all
        (snap_mod._UNLOADED, ["a"],      {"a"}),         # never loaded, want one -> read one
        (None,               ["a"],      "COVERED"),     # all cached -> nothing to do
        (None,               None,       "COVERED"),
        ({"a", "b"},         ["a"],      "COVERED"),     # subset of cache -> nothing to do
        ({"a", "b"},         ["a", "b"], "COVERED"),
        ({"a"},              ["b"],      {"a", "b"}),    # new column -> read the union
        ({"a"},              None,       None),          # widening to all -> read all
    ],
    ids=["fresh-all", "fresh-narrow", "cached-all", "cached-all-all",
         "subset", "exact", "union", "widen"],
)
def test_columns_to_load(cached, asked, expected):
    snap = Snapshot()
    if cached is not snap_mod._UNLOADED:
        snap._loaded_features["T"] = cached
    got = snap._columns_to_load("T", asked)
    if expected == "COVERED":
        assert got is snap_mod._COVERED
    else:
        assert got == expected


# The ROI graph.
# parent_roi round-trips as the parent's name; the index resolves it and never guesses.
SLIDE = _Rec("uS", "arutha", "WholeSlide")
TMA0 = _Rec("uT0", "arutha:TMA0", "TMA")
TMA1 = _Rec("uT1", "arutha:TMA1", "TMA")
C1 = _Rec("uC1", "arutha:TMA0:R0C0", "Core")
C2 = _Rec("uC2", "arutha:TMA0:R0C1", "Core")
C3 = _Rec("uC3", "arutha:TMA1:R0C0", "Core")


def _tree(extra_values: dict[str, dict] | None = None) -> Snapshot:
    values = {
        "uT0": {"parent_roi": "arutha"},
        "uT1": {"parent_roi": "arutha"},
        "uC1": {"parent_roi": "arutha:TMA0"},
        "uC2": {"parent_roi": "arutha:TMA0"},
        "uC3": {"parent_roi": "arutha:TMA1"},
    }
    values.update(extra_values or {})
    return _loaded(
        {"WholeSlide": [SLIDE], "TMA": [TMA0, TMA1], "Core": [C1, C2, C3]}, values)


def test_children_resolves_the_name_string_parent_roi_writes():
    assert [r.name for r in _tree().children(TMA0)] == ["arutha:TMA0:R0C0", "arutha:TMA0:R0C1"]


def test_children_are_name_sorted_so_a_plan_is_reproducible():
    snap = _loaded({"TMA": [TMA0], "Core": [C2, C1]},
                   {"uC1": {"parent_roi": "arutha:TMA0"}, "uC2": {"parent_roi": "arutha:TMA0"}})
    assert [r.name for r in snap.children(TMA0)] == ["arutha:TMA0:R0C0", "arutha:TMA0:R0C1"]


def test_children_can_narrow_to_one_subtype():
    snap = _tree()
    assert [r.name for r in snap.children(SLIDE, roi_type="TMA")] == [
        "arutha:TMA0", "arutha:TMA1"]
    assert snap.children(SLIDE, roi_type="Core") == []


def test_parent_walks_back_up():
    snap = _tree()
    assert snap.parent(C1).name == "arutha:TMA0"
    assert snap.parent(SLIDE) is None


def test_descendants_spans_more_than_one_level():
    assert [r.name for r in _tree().descendants(SLIDE, roi_type="Core")] == [
        "arutha:TMA0:R0C0", "arutha:TMA0:R0C1", "arutha:TMA1:R0C0"]


def test_descendants_terminates_on_a_cycle():
    a, b = _Rec("ua", "a", "Core"), _Rec("ub", "a:b", "Core")
    snap = _loaded({"Core": [a, b]}, {"ua": {"parent_roi": "a:b"}, "ub": {"parent_roi": "a"}})
    assert [r.name for r in snap.descendants(a)] == ["a:b"]


def test_an_unresolvable_parent_is_warned_about_not_guessed():
    snap = _tree({"uC1": {"parent_roi": "arutha:TMA9"}})
    assert [r.name for r in snap.children(TMA0)] == ["arutha:TMA0:R0C1"]
    assert any("TMA9" in w for w in snap.graph_warnings())


def test_a_duplicate_name_makes_parentage_ambiguous_rather_than_wrong():
    dupe = _Rec("uT0b", "arutha:TMA0", "TMA")
    snap = _loaded({"TMA": [TMA0, dupe], "Core": [C1]}, {"uC1": {"parent_roi": "arutha:TMA0"}})
    assert snap.children(TMA0) == []
    assert any("not unique" in w for w in snap.graph_warnings())


def test_a_merge_target_with_many_parents_is_reported_not_placed():
    snap = _tree({"uC1": {"parent_roi": ["arutha:TMA0", "arutha:TMA1"]}})
    assert [r.name for r in snap.children(TMA0)] == ["arutha:TMA0:R0C1"]
    assert any("merge target" in w for w in snap.graph_warnings())


def test_a_parent_that_arrived_as_an_object_is_matched_on_uid():
    snap = _tree({"uC1": {"parent_roi": TMA0}})
    assert [r.name for r in snap.children(TMA0)] == ["arutha:TMA0:R0C0", "arutha:TMA0:R0C1"]


# Per-record indexes.
def test_roles_of_inverts_the_flat_triple_list():
    snap = _loaded({"Core": [C1, C2]})
    snap._role_artifacts = [
        (C1, "bgnormed", "a.zarr"), (C1, "segmentations", "b.tif"),
        (C1, "bgnormed", "a2.zarr"), (C2, "bgnormed", "c.zarr"),
    ]
    assert snap.roles_of(C1) == {"bgnormed": ("a.zarr", "a2.zarr"), "segmentations": ("b.tif",)}
    assert snap.roles_of(C2) == {"bgnormed": ("c.zarr",)}


def test_roles_of_is_empty_for_a_record_with_no_role_artifacts():
    snap = _loaded({"Core": [C1]})
    snap._role_artifacts = []
    assert snap.roles_of(C1) == {}


def test_record_by_name_indexes_once_per_type():
    snap = _loaded({"Core": [C1, C2]})
    assert snap.record_by_name("Core", "arutha:TMA0:R0C1") is C2
    assert snap.record_by_name("Core", "nope") is None


def test_record_by_name_keeps_the_first_of_a_duplicate_rather_than_raising():
    dupe = _Rec("uC1b", "arutha:TMA0:R0C0", "Core")
    snap = _loaded({"Core": [C1, dupe]})
    assert snap.record_by_name("Core", "arutha:TMA0:R0C0") is C1
