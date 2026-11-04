"""Corpus survey and job tests: a survey agrees with describe; a partly blocked job builds."""
from __future__ import annotations

import pytest

from clinicalomx.extract.app import demo
from clinicalomx.extract.availability import describe
from clinicalomx.extract.corpus import summarise, survey
from clinicalomx.extract.jobs import assemble_job, plan_dataset
from clinicalomx.extract.plan import nearest_buildable
from clinicalomx.extract.request import Destination, ExtractRequest, ROISelector

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def snap():
    demo.install_governance()
    return demo.build_snapshot()


def _request(**kw):
    base = {"roi": ROISelector(name=demo.TMA_NAME, include="children", child_type="Core"),
            "target": "OME-TIFF",
            "destination": Destination(kind="path", path="./out"),
            "strictness": "degrade"}
    return ExtractRequest(**{**base, **kw})


# The survey agrees with describe.
def test_a_survey_counts_the_same_things_a_describe_does(snap):
    sv = survey(snapshot=snap, roi_types=["Core"], now=NOW)
    survey_present = {c.role: c.present for c in sv.coverage}

    per_parent: dict[str, int] = {}
    for tma in snap.records("TMA"):
        for cov in describe(tma, snapshot=snap, now=NOW).child_coverage:
            per_parent[cov.role] = per_parent.get(cov.role, 0) + cov.present

    for role, present in per_parent.items():
        assert survey_present.get(role, 0) == present, role


def test_a_survey_reports_the_population_by_type(snap):
    # The fixture includes a second TMA, its core, and an unplaced TissueSection.
    assert survey(snapshot=snap, now=NOW).population == {
        "WholeSlide": 1, "TMA": 2, "Core": demo.N_CORES + 1, "TissueSection": 1}


def test_a_survey_answers_what_could_be_built(snap):
    reach = {r.recipe: r for r in survey(snapshot=snap, now=NOW).recipes}
    assert reach["ometiff.core"].buildable > 0
    assert reach["ometiff.core"].blocked > 0, "the fixture has cores that cannot build"
    assert reach["ometiff.core"].examples, "instances, not just a count"


def test_a_survey_never_claims_zero_missing_without_probing(snap):
    sv = survey(snapshot=snap, now=NOW)
    assert sv.bytes_checked is False
    line = next(ln for ln in summarise(sv).splitlines() if "source_image" in ln)
    assert " - " in line


def test_a_survey_leads_with_its_own_staleness(snap):
    assert "Re-survey" in survey(snapshot=snap, now=NOW).caveats()[0]


def test_a_survey_can_group(snap):
    sv = survey(snapshot=snap, group_by="roi_type", now=NOW)
    assert set(sv.groups) == {"Core", "TMA", "TissueSection", "WholeSlide"}
    assert sv.group_by == "roi_type"


def test_a_survey_is_json_serialisable(snap):
    import json

    json.dumps(survey(snapshot=snap, group_by="slide", now=NOW).to_dict())


# Jobs.
def test_a_job_plans_every_matched_roi(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    assert len(job.plans) == demo.N_CORES


def test_a_partly_unbuildable_job_still_builds_the_rest(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    assert job.units, "some cores are buildable"
    assert job.unbuildable, "and some are not"
    _report, gated = job.summarise()
    assert gated is False, "a partly-buildable dataset is not blocked"


def test_a_shrunken_job_names_what_produced_nothing(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    report, _ = job.summarise()
    assert "produce nothing" in report
    # The unplaceable core: a core with no artifacts of its own still has its slide's pixels.
    assert any(demo.UNPLACEABLE_CORE == int(n.rsplit("C", 1)[-1]) for n in job.unbuildable)


def test_a_job_with_nothing_to_build_is_gated(snap):
    job = plan_dataset(
        _request(roi=ROISelector(name=f"{demo.TMA_NAME}:R0C{demo.UNPLACEABLE_CORE}")),
        intent="a core nothing can position", snapshot=snap, now=NOW)
    assert not job.units
    with pytest.raises(Exception, match="build nothing"):
        assemble_job(job, confirm=True, force_stale=True)


def test_a_job_token_is_stable_and_moves_with_its_members(snap):
    a = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    b = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    c = plan_dataset(_request(), intent="a different ask", snapshot=snap, now=NOW)
    assert a.token == b.token and a.token != c.token
    assert a.token.startswith("job_")


def test_a_job_refuses_without_confirmation(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    with pytest.raises(Exception, match="confirm"):
        assemble_job(job, confirm=False)


def test_a_stale_job_refuses(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap,
                       now="2020-01-01T00:00:00Z")
    with pytest.raises(Exception, match="stale|no longer describe"):
        assemble_job(job, confirm=True)


def test_a_job_states_its_cost_before_anybody_agrees(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    est = job.estimate()
    assert est["units"] == len(job.units)
    assert est["cores"] == len(job.units), "a per-child unit IS one core"
    assert "not a laptop operation" in est["note"]
    assert "GB of RAM per core" in job.summarise()[0]


def test_a_merged_unit_is_costed_as_the_cores_it_reads(snap):
    from clinicalomx.extract.jobs import CORE_BUDGET_MINUTES, Job
    from clinicalomx.extract.plan import Unit

    elements = {"raw": "source_image", "bgnorm": "bgnormed"}
    unit = Unit(roi_uid="u0", roi_name="slide:TMA0", output="out.ome.zarr", elements=elements,
                child_elements={f"R0C{i}": dict(elements) for i in range(72)})

    class _Plan:
        buildable = [unit]

    est = Job(intent="one plate", request={}, plans=[_Plan()]).estimate()
    assert est["units"] == 1, "it is still one output, which is what status is keyed on"
    assert est["cores"] == 72
    assert est["arrays"] == 144, "one crop per element per child, not per unit"
    assert est["rough_hours"] == round(72 * CORE_BUDGET_MINUTES / 60, 1)
    assert "72 core(s) in 1 output(s)" in est["note"]
    assert "RAM" in est["note"] and "Scratch" in est["note"]


def _plate(i: int, cores: int = 72):
    from clinicalomx.extract.plan import Unit

    elements = {"raw": "source_image", "bgnorm": "bgnormed"}
    return Unit(roi_uid=f"u{i}", roi_name=f"slide:TMA{i}", output=f"p{i}.ome.zarr",
                elements=elements,
                child_elements={f"R0C{j}": dict(elements) for j in range(cores)})


def test_a_merged_unit_is_measured_against_the_runs_it_needs():
    from clinicalomx.extract.jobs import Job

    class _Plan:
        buildable = [_plate(i) for i in range(4)]
        token, roi_name, layout = "t", "slide", "merged"

    est = Job(intent="", request={}, plans=[_Plan()]).estimate(per_run=4)
    assert est["runs"] == 4, "each plate rides alone"
    assert est["over_run_budget"] == [f"p{i}.ome.zarr" for i in range(4)]
    assert "cannot be split" in est["note"] and "72" in est["note"]


def test_scratch_is_in_every_report_even_when_nothing_measured_it():
    class _Plan:
        buildable = [_plate(0)]
        token, roi_name, layout = "t", "slide", "merged"

    from clinicalomx.extract.jobs import Job

    job = Job(intent="", request={}, plans=[_Plan()])
    est = job.estimate()
    assert est["scratch_bytes"] is None and est["scratch_basis"]
    assert "Scratch is NOT measured" in est["note"]
    assert "scratch  NOT MEASURED" in job.summarise()[0]


def test_every_unit_starts_pending(snap):
    job = plan_dataset(_request(), intent="every core", snapshot=snap, now=NOW)
    assert set(job.status.values()) == {"pending"}
    assert len(job.status) == len(job.units)


# Grounded alternatives.
def test_alternatives_are_resolved_not_guessed(snap):
    from clinicalomx.extract import all_recipes

    known = {r.name for r in all_recipes()}
    req = _request(roi=ROISelector(name=f"{demo.TMA_NAME}:R0C0"), target="SpatialData")
    alts = nearest_buildable(req, snapshot=snap, now=NOW)
    assert alts
    assert all(a["recipe"] in known for a in alts)
    assert all(a["verdict"] in ("ready", "degraded", "blocked") for a in alts)


def test_alternatives_put_what_would_build_first(snap):
    req = _request(roi=ROISelector(name=f"{demo.TMA_NAME}:R0C0"), target="SpatialData")
    verdicts = [a["verdict"] for a in nearest_buildable(req, snapshot=snap, now=NOW)]
    assert verdicts == sorted(verdicts, key=["ready", "degraded", "blocked"].index)


def test_alternatives_say_why_a_blocked_one_is_blocked(snap):
    req = _request(roi=ROISelector(name=f"{demo.TMA_NAME}:R0C{demo.UNPLACEABLE_CORE}"),
                   target="SpatialData")
    alts = nearest_buildable(req, snapshot=snap, now=NOW)
    assert all(a["verdict"] == "blocked" for a in alts)
    assert any(a["why_blocked"] for a in alts)


# The tool surface for datasets.
def test_a_job_response_leads_with_the_cost(snap, monkeypatch):
    from clinicalomx.extract import tools
    from clinicalomx.extract.jobs import plan_dataset as build

    tools.forget_plans()
    monkeypatch.setattr("clinicalomx.extract.jobs.plan_dataset",
                        lambda req, **kw: build(req, snapshot=snap, now=NOW, **kw))
    got = tools.plan_dataset(_request().model_dump(exclude_none=True), intent="every core")
    assert got["ok"] is True
    assert "BEFORE they agree" in got["caveats"][0]
    assert "not a laptop operation" in got["caveats"][0]
    tools.forget_plans()


def test_a_job_response_reports_the_shortfall(snap, monkeypatch):
    from clinicalomx.extract import tools
    from clinicalomx.extract.jobs import plan_dataset as build

    tools.forget_plans()
    monkeypatch.setattr("clinicalomx.extract.jobs.plan_dataset",
                        lambda req, **kw: build(req, snapshot=snap, now=NOW, **kw))
    got = tools.plan_dataset(_request().model_dump(exclude_none=True), intent="every core")
    assert any("produce nothing and will be skipped" in c for c in got["caveats"])
    tools.forget_plans()


def test_assemble_job_refuses_a_token_it_never_issued():
    from clinicalomx.extract import tools

    tools.forget_plans()
    got = tools.assemble_job("job_deadbeef", confirm=True)
    assert got["ok"] is False and "no job with token" in got["error"]


def test_assemble_job_requires_confirmation_and_shows_the_estimate(snap, monkeypatch):
    from clinicalomx.extract import tools
    from clinicalomx.extract.jobs import plan_dataset as build

    tools.forget_plans()
    monkeypatch.setattr("clinicalomx.extract.jobs.plan_dataset",
                        lambda req, **kw: build(req, snapshot=snap, now=NOW, **kw))
    token = tools.plan_dataset(_request().model_dump(exclude_none=True))["data"]["token"]
    got = tools.assemble_job(token, confirm=False)
    assert got["ok"] is False and got["error"] == "not confirmed"
    assert "estimate" in got["data"] and "cost" in got["hint"].lower()
    tools.forget_plans()


def test_job_status_is_unavailable_for_an_unknown_token():
    from clinicalomx.extract import tools

    tools.forget_plans()
    assert tools.job_status("job_nope")["ok"] is False


def test_a_search_at_the_limit_says_it_is_probably_truncated():
    from clinicalomx.extract import tools

    got = tools.search_rois({"roi_type": "Core", "limit": 1})
    if got["ok"]:
        assert any("limit" in c for c in got["caveats"]) or got["data"]["n"] < 1


def test_suggest_alternatives_tells_the_agent_not_to_invent(snap, monkeypatch):
    from clinicalomx.extract import tools

    monkeypatch.setattr("clinicalomx.extract.plan.nearest_buildable",
                        lambda req, **kw: [{"recipe": "ometiff.core", "verdict": "ready"}])
    got = tools.suggest_alternatives(
        _request(roi=ROISelector(name=f"{demo.TMA_NAME}:R0C0")).model_dump(exclude_none=True))
    assert got["ok"] is True
    assert any("must not be suggested" in c for c in got["caveats"])
