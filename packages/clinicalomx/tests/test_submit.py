"""Tests for submitting a plan: the payload round-trips its token, and a mismatched recipe or wire
version is refused."""
from __future__ import annotations

from dataclasses import replace

import pytest

from clinicalomx.extract import (
    Element,
    Recipe,
    register_recipes,
    reset_registrations,
)
from clinicalomx.extract.availability import Availability, ROIRef
from clinicalomx.extract.plan import Plan, Unit
from clinicalomx.extract.submit import (
    TAG_PREFIX,
    Run,
    SubmitError,
    _walltime_minutes,
    chunks_for,
    payload_for,
    register_run_reader,
    register_submitter,
    rehydrate,
    restrict,
    runs,
    submit,
    units_per_run,
)

RECIPE = Recipe(name="t.hop", target="OME-TIFF", roi_types=("Core",), layout="per_child",
                elements=(Element("image", "image", roles=("source_image",)),),
                builder="clinicalomx.extract.builders.ometiff:build_core",
                placement="clinicalomx.extract.coords:BboxPlacement",
                child_types=("Core",))


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _recipe_available():
    """Make the recipe resolvable by name, as `assemble` resolves it."""
    register_recipes([RECIPE], provider="test")
    yield
    reset_registrations()


def _unit(n: int) -> Unit:
    name = f"s:TMA0:R0C{n}"
    return Unit(f"u{n}", name, f"./out/{name}.ome.tiff", {"image": "source_image"})


def _plan(n_units: int = 3, **kw) -> Plan:
    now = _now()
    return Plan(recipe="t.hop", target="OME-TIFF", layout="per_child", roi_name="s:TMA0",
                availability=Availability(
                    roi=ROIRef("u0", "s:TMA0", "TMA"), subtree={"Core": n_units}, roles=(),
                    child_coverage=(), recipes=(), bytes_checked=False,
                    installed_consumers=("clinicalomx",), index_built_at=now),
                units=[_unit(i) for i in range(n_units)],
                recipe_content=RECIPE.to_dict(),
                request={"target": "OME-TIFF", "downsample": 1,
                         "destination": {"kind": "path", "path": "./out"}},
                index_built_at=now, **kw)


# The wire form.
def test_the_payload_is_json():
    import json

    json.dumps(payload_for(_plan()))


def test_a_rehydrated_plan_carries_the_same_token():
    p = _plan()
    assert rehydrate(payload_for(p)).token == p.token


def test_a_rehydrated_plan_builds_the_same_units():
    p = _plan()
    back = rehydrate(payload_for(p))
    assert [u.to_dict() for u in back.units] == [u.to_dict() for u in p.units]
    assert back.recipe == p.recipe and back.request == p.request


def test_an_edited_payload_is_refused():
    payload = payload_for(_plan())
    payload["units"][0]["output"] = "./out/somewhere-else.ome.tiff"
    with pytest.raises(SubmitError, match="does not hash to the token"):
        rehydrate(payload)


def test_a_payload_from_another_wire_version_is_refused():
    payload = payload_for(_plan())
    payload["wire_version"] = 99
    with pytest.raises(SubmitError, match="wire version"):
        rehydrate(payload)


def test_a_recipe_this_environment_does_not_have_is_refused():
    payload = payload_for(_plan())
    reset_registrations()
    with pytest.raises(SubmitError, match="no recipe named"):
        rehydrate(payload)


def test_a_recipe_that_differs_here_is_refused():
    payload = payload_for(_plan())
    reset_registrations()
    register_recipes([replace(RECIPE, builder="clinicalomx.extract.builders.ometiff:build_merged")],
                     provider="test")
    with pytest.raises(SubmitError, match="not the recipe this plan was built from"):
        rehydrate(payload)


def test_the_stand_in_availability_answers_only_artifact_keys():
    back = rehydrate(payload_for(_plan()))
    assert back.artifact_keys == sorted(_plan().artifact_keys)
    with pytest.raises(AttributeError):
        _ = back.availability.subtree


# Chunking.
def test_chunking_drops_nothing():
    plan = _plan(n_units=7)
    got = [o for c in chunks_for(plan, per_run=3) for o in c.outputs]
    assert got == [u.output for u in plan.buildable]


def test_chunking_is_deterministic():
    plan = _plan(n_units=7)
    assert ([c.outputs for c in chunks_for(plan, per_run=3)]
            == [c.outputs for c in chunks_for(plan, per_run=3)])


def test_every_chunk_carries_the_whole_plan_and_its_token():
    plan = _plan(n_units=5)
    for chunk in chunks_for(plan, per_run=2):
        assert chunk.token == plan.token
        assert rehydrate(chunk.payload).token == plan.token
        assert chunk.tag == TAG_PREFIX + plan.token


def test_a_plan_that_would_build_nothing_is_not_submittable():
    plan = _plan(n_units=1)
    plan.units[0] = replace(plan.units[0], verdict="skip")
    with pytest.raises(SubmitError, match="build nothing"):
        chunks_for(plan, per_run=4)


def _merged(n: int, cores: int) -> Unit:
    name = f"s:TMA{n}"
    return Unit(f"m{n}", name, f"./out/{name}.ome.zarr", {"image": "source_image"},
                child_elements={f"{name}:R0C{i}": {"image": "source_image"}
                                for i in range(cores)})


def test_a_merged_unit_is_chunked_by_the_cores_it_reads():
    plan = replace(_plan(n_units=1), layout="merged", units=[_merged(i, 72) for i in range(4)])
    assert [len(c.outputs) for c in chunks_for(plan, per_run=4)] == [1, 1, 1, 1]

    small = replace(plan, units=[_merged(i, 2) for i in range(5)])
    assert [len(c.outputs) for c in chunks_for(small, per_run=4)] == [2, 2, 1]


def test_a_per_child_plan_chunks_exactly_as_it_did():
    plan = _plan(n_units=9)
    assert [len(c.outputs) for c in chunks_for(plan, per_run=4)] == [4, 4, 1]


def test_units_per_run_follows_the_stated_envelope():
    from clinicalomx.extract.jobs import CORE_BUDGET_MINUTES

    assert units_per_run(walltime_minutes=6 * 60) == (6 * 60) // CORE_BUDGET_MINUTES
    assert units_per_run(walltime_minutes=10) == 1


# Restricting a plan to one chunk.
def test_restrict_keeps_the_units_verbatim():
    plan = _plan(n_units=4)
    part = restrict(plan, [plan.units[1].output, plan.units[2].output])
    assert [u.to_dict() for u in part.units] == [plan.units[1].to_dict(), plan.units[2].to_dict()]


def test_restrict_changes_the_token_and_that_is_the_point():
    plan = _plan(n_units=4)
    assert restrict(plan, [plan.units[0].output]).token != plan.token


def test_restrict_refuses_an_output_the_plan_does_not_carry():
    with pytest.raises(SubmitError, match="not in this plan"):
        restrict(_plan(), ["./out/invented.ome.tiff"])


# The injected orchestrator.
def test_submit_hands_one_chunk_at_a_time_to_the_registered_submitter():
    seen = []

    def spy(chunk):
        seen.append(chunk)
        return Run(id=str(len(seen)), name=f"run-{len(seen)}", state="SCHEDULED",
                   token=chunk.token, outputs=chunk.outputs)

    register_submitter(spy)
    try:
        out = submit(_plan(n_units=5), per_run=2)
    finally:
        register_submitter(None)
    assert len(seen) == 3 and [r.state for r in out] == ["SCHEDULED"] * 3


def test_submitting_with_nothing_registered_says_what_to_do():
    register_submitter(None)
    with pytest.raises(SubmitError, match="use_prefect"):
        submit(_plan(), per_run=4)


def test_runs_is_empty_rather_than_broken_when_no_reader_is_registered():
    register_run_reader(None)
    assert runs() == []


def test_the_app_never_imports_prefect_to_read_this_module():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import clinicalomx.extract.submit, sys; print('prefect' in sys.modules)"],
        capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# Reading the envelope off the deployment rather than mirroring it.
@pytest.mark.parametrize("raw,minutes", [
    ("6:00:00", 360),
    ("0:10:00", 10),
    ("1-00:00:00", 24 * 60),
    ("90", 90),          # SLURM reads a bare number as minutes.
    ("", None),
    (None, None),
    ("soon", None),
])
def test_a_slurm_walltime_becomes_minutes(raw, minutes):
    assert _walltime_minutes(raw) == minutes


def test_can_submit_is_false_with_nothing_registered():
    from clinicalomx.extract.submit import can_submit

    register_submitter(None)
    assert can_submit() is False


# A deployment read that fails says WHICH way it failed.
class _Client:
    """A Prefect client whose deployment read fails, and whose healthcheck answers or not."""

    def __init__(self, *, healthy: bool):
        self.healthy = healthy

    def read_deployment_by_name(self, name):
        raise RuntimeError(f"no deployment {name}")

    def api_healthcheck(self):
        return None if self.healthy else ConnectionError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_reachable_server_missing_the_deployment_says_so(monkeypatch):
    from clinicalomx.extract import submit as hop

    monkeypatch.setenv("PREFECT_API_URL", "http://clinicalomxdb_prefect:4200/api")
    monkeypatch.setattr(hop, "_client", lambda: _Client(healthy=True))
    with pytest.raises(SubmitError) as exc:
        hop.prefect_walltime_minutes()
    assert "not registered" in str(exc.value)
    assert "http://clinicalomxdb_prefect:4200/api" in str(exc.value)


def test_an_unreachable_server_is_not_reported_as_a_missing_deployment(monkeypatch):
    from clinicalomx.extract import submit as hop

    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    monkeypatch.setattr(hop, "_client", lambda: _Client(healthy=False))
    with pytest.raises(SubmitError) as exc:
        hop.prefect_walltime_minutes()
    assert "cannot be reached" in str(exc.value)
    assert "PREFECT_API_URL is not set" in str(exc.value)


def test_a_client_too_old_to_healthcheck_reads_as_unreachable(monkeypatch):
    from clinicalomx.extract import submit as hop

    class _NoHealthcheck(_Client):
        def api_healthcheck(self):
            raise AttributeError("no such method")

    monkeypatch.setattr(hop, "_client", lambda: _NoHealthcheck(healthy=False))
    with pytest.raises(SubmitError, match="cannot be reached"):
        hop.prefect_walltime_minutes()


def test_submitting_against_an_unreachable_server_is_a_submit_error_too(monkeypatch):
    from clinicalomx.extract import submit as hop

    monkeypatch.setattr(hop, "_client", lambda: _Client(healthy=True))
    with pytest.raises(SubmitError, match="not registered"):
        hop.prefect_submitter(chunks_for(_plan(), per_run=4)[0])
