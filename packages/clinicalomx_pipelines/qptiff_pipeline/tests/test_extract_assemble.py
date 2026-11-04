"""extract-assemble: the deployment, and what the flow may not do.

The flow must not re-plan: an AST scan checks it never reaches the request-resolving functions. The
deployment must run on the one image that installs ``clinicalomx[extract]``.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import sys
import types

import pytest
import yaml

from conftest import load_flow_module

PREFECT_YAML = pathlib.Path(__file__).resolve().parents[1] / "prefect.yaml"
FLOW = pathlib.Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline" / "flows" \
    / "extract_assemble.py"
DEPLOYMENT = "pipeline-extract-assemble"

# SLURM's memory units, in kibibytes. A bare number means megabytes to `--mem`.
_MEM_KIB = {"K": 1, "": 1024, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}


def _mem_kib(mem: str) -> int:
    mem = mem.strip().upper()
    suffix = mem[-1] if mem[-1] in _MEM_KIB else ""
    return int(mem[: len(mem) - len(suffix)]) * _MEM_KIB[suffix]


@pytest.fixture(scope="module")
def deployment() -> dict:
    doc = yaml.safe_load(PREFECT_YAML.read_text())
    found = next((d for d in doc["deployments"] if d["name"] == DEPLOYMENT), None)
    assert found is not None, f"{DEPLOYMENT} is not declared in prefect.yaml"
    return found


@pytest.fixture(scope="module")
def submit():
    """The framework's side of the contract, from the installed wheel. Skipped if it predates it."""
    return pytest.importorskip(
        "clinicalomx.extract.submit",
        reason="the hop needs a clinicalomx carrying extract.submit")


def test_the_flow_imports_with_only_an_orchestrator():
    mod = load_flow_module("extract_assemble")  # first, so the stub is installed without prefect
    import prefect

    if not hasattr(prefect, "flow"):
        pytest.skip("a review test module installed its fake prefect; run the flow tests alone")
    assert mod.extract_assemble is not None


def test_the_flow_never_re_plans():
    forbidden = {"clinicalomx.extract.plan", "clinicalomx.extract.select",
                 "clinicalomx.extract.availability", "clinicalomx.extract.jobs",
                 "clinicalomx.extract.corpus"}
    reached = set()
    for node in ast.walk(ast.parse(FLOW.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("clinicalomx"):
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached |= {a.name for a in node.names if a.name.startswith("clinicalomx")}
    assert not (reached & forbidden), (
        f"the assemble flow reaches the planner: {sorted(reached & forbidden)}. It must rehydrate "
        f"the approved plan, never resolve a request again.")


def test_the_flow_registers_this_repos_governance_table():
    assert "declared.register()" in FLOW.read_text()


# The deployment.
def test_the_deployment_runs_the_only_image_that_carries_the_builders(deployment):
    jv = deployment["work_pool"]["job_variables"]
    assert "comx-extract:" in jv["image"], jv["image"]
    assert jv["sif_artifact"].startswith("environments/{{ $CLINICALOMX_SITE_IMAGE_OWNER }}/comx-extract_")


def test_the_deployment_is_human_invoked(deployment):
    assert deployment.get("schedules") == []


def test_concurrency_is_bounded_by_declaration_and_not_by_a_monitor(deployment):
    assert deployment.get("concurrency_limit", 0) > 0


def test_the_soft_budget_stays_under_the_walltime(deployment):
    from clinicalomx.extract.submit import _walltime_minutes

    walltime = _walltime_minutes(deployment["work_pool"]["job_variables"]["walltime"])
    assert walltime is not None
    assert 0 < deployment["parameters"]["max_runtime_minutes"] < walltime


def test_the_chunk_size_follows_from_the_declared_walltime(deployment, submit):
    walltime = submit._walltime_minutes(deployment["work_pool"]["job_variables"]["walltime"])
    assert submit.units_per_run(walltime_minutes=walltime) == 4


def test_the_declared_memory_covers_the_budget_the_estimate_quotes(deployment):
    from clinicalomx.extract.jobs import CORE_BUDGET_GB

    declared = deployment["work_pool"]["job_variables"]["mem"]
    assert declared.endswith("G"), f"the memory request {declared!r} is not in whole gigabytes"
    assert int(declared[:-1]) >= CORE_BUDGET_GB, (
        f"the deployment requests {declared} while a user is promised {CORE_BUDGET_GB}G a unit")


def test_the_oom_ceiling_is_declared_and_is_above_the_request(deployment):
    jv = deployment["work_pool"]["job_variables"]
    ceiling = jv.get("oom_retry_max_mem")
    assert ceiling == "512G", (
        f"the OOM ceiling reads {ceiling!r}; 512G is the maximum the captain set")
    declared = jv["mem"]
    assert _mem_kib(ceiling) > _mem_kib(declared), (
        f"the ceiling {ceiling} leaves no room to double from {declared}")


def test_the_app_submits_to_the_deployment_that_is_declared(deployment, submit):
    assert submit.DEPLOYMENT == f"{submit.FLOW_NAME}/{deployment['name']}"
    assert deployment["entrypoint"].endswith("extract_assemble.py:extract_assemble")
    # Read the flow name from the decorator: the prefect stub drops it. `runs()` filters by it.
    assert f'@flow(name="{submit.FLOW_NAME}"' in FLOW.read_text()


# A run that built nothing.
class _Unit:
    def __init__(self, roi_name):
        self.roi_name = roi_name
        self.output = f"out/{roi_name.replace(':', '_')}.ome.tiff"


class _Plan:
    """The parts of a rehydrated `Plan` the flow reads."""

    def __init__(self, units, token="plan-token"):
        self.buildable = units
        self.token = token
        self.recipe = "ometiff.tma.mosaic"
        self.target = "ometiff"
        self.roi_name = "s:TMA0"
        self.index_built_at = ""

    def summarise(self):
        return ("",)


@pytest.fixture
def run_flow(monkeypatch):
    """Run the flow body with the builder's answer per unit supplied by the test.

    Every database, Prefect and storage import is replaced. ``assemble`` keeps the real contract.
    """
    published = []

    def install(outcomes: dict):
        def assemble(one, **kw):
            (unit,) = one.buildable
            outcome = outcomes[unit.roi_name]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return {"built": [{"roi_name": unit.roi_name, "output": unit.output}],
                        "n_built": 1, "n_registered": 0, "failed": []}
            return {"built": [], "n_built": 0, "n_registered": 0,
                    "failed": [f"{unit.roi_name}: ValueError: {outcome}"]}

        def restrict(plan, outputs):
            return _Plan([u for u in plan.buildable if u.output in outputs], plan.token)

        fakes = {
            "lamindb": {"finish": lambda: None},
            "clinicalomx.extract.assemble": {"assemble": assemble},
            "clinicalomx.extract.submit": {
                "rehydrate": lambda payload: _Plan([_Unit(n) for n in outcomes]),
                "restrict": restrict},
            "clinicalomx.snapshot": {"Snapshot": lambda: object()},
            "qptiff_pipeline.domain.governance": {"register": lambda: None},
            "qptiff_pipeline.lamin.tracking": {"track_prefect_run": lambda stage: None},
        }
        for name, attrs in fakes.items():
            mod = types.ModuleType(name)
            mod.__dict__.update(attrs)
            monkeypatch.setitem(sys.modules, name, mod)

        mod = load_flow_module("extract_assemble")
        monkeypatch.setattr(mod, "get_run_logger", lambda: logging.getLogger("extract-test"))
        monkeypatch.setattr(mod, "_publish", lambda plan, part, result: published.append(result))
        # `.fn` is the undecorated body under real prefect; the stub returns it bare.
        body = getattr(mod.extract_assemble, "fn", mod.extract_assemble)
        install.flow = mod.extract_assemble
        return lambda: body(payload={})

    install.published = published
    return install


def test_a_merged_unit_the_builder_refuses_fails_the_run(run_flow):
    go = run_flow({"s:TMA0": "would STORE 171.5 GB, over the 8.0 GB ceiling"})
    with pytest.raises(RuntimeError, match=r"built nothing.*over the 8\.0 GB ceiling"):
        go()
    # The table artifact is still written.
    assert run_flow.published[0]["failed"] == [
        "s:TMA0: ValueError: would STORE 171.5 GB, over the 8.0 GB ceiling"]


def test_a_merged_unit_that_raises_out_of_assemble_fails_the_run(run_flow):
    go = run_flow({"s:TMA0": ValueError("destination /nope is not a declared folder")})
    with pytest.raises(RuntimeError, match="destination /nope is not a declared folder"):
        go()


def _orchestrated(flow):
    """Run the real flow on an ephemeral Prefect server. Returns its state and run count."""
    from prefect import get_client
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        state = flow.with_options(retry_delay_seconds=0)(payload={}, return_state=True)
        with get_client(sync_client=True) as client:
            run = client.read_flow_run(state.state_details.flow_run_id)
    return state, run.run_count


def test_the_nothing_built_refusal_is_not_retried(run_flow):
    pytest.importorskip("prefect.testing.utilities")
    run_flow({"s:TMA0": "would STORE 171.5 GB, over the 8.0 GB ceiling"})
    state, run_count = _orchestrated(run_flow.flow)
    assert state.is_failed()
    assert "over the 8.0 GB ceiling" in state.message
    assert run_count == 1


def test_any_other_raise_is_still_retried(run_flow):
    pytest.importorskip("prefect.testing.utilities")
    run_flow({})
    state, run_count = _orchestrated(run_flow.flow)
    assert state.is_failed()
    assert run_count == 2


def test_one_bad_unit_in_a_chunk_does_not_lose_the_rest(run_flow):
    go = run_flow({"s:TMA0:R0C0": None, "s:TMA0:R0C1": "unreadable", "s:TMA0:R0C2": None})
    result = go()
    assert result["n_built"] == 2
    assert result["failed"] == ["s:TMA0:R0C1: ValueError: unreadable"]


def test_a_chunk_in_which_every_unit_failed_fails_the_run(run_flow):
    go = run_flow({"s:TMA0:R0C0": "unreadable", "s:TMA0:R0C1": "unreadable"})
    with pytest.raises(RuntimeError, match=r"2 unit\(s\) failed"):
        go()
