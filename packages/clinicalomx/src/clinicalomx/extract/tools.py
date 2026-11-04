"""The JSON-in, JSON-out surface an agent drives, and the only one it may.

The agent's writable input is a dict validated by
:class:`~clinicalomx.extract.request.ExtractRequest`. ``assemble_extract`` takes a plan token,
never a request. Every response is ``{ok, kind, data, caveats, provenance}``; ``caveats`` is
always populated. No function here raises.
"""
from __future__ import annotations

from typing import Any

from clinicalomx.extract import providers
from clinicalomx.extract.assemble import assemble
from clinicalomx.extract.availability import describe
from clinicalomx.extract.corpus import summarise, survey
from clinicalomx.extract.jobs import assemble_job as run_job

#: Plans this process produced, by token. In-process only.
_PLANS: dict[str, Any] = {}


def _provenance(*, index_built_at: str = "", bytes_checked: bool = False) -> dict:
    from clinicalomx import __version__
    from clinicalomx.extract.source import default_source

    return {
        "clinicalomx_version": __version__,
        "recipe_providers": list(providers()),
        "index_built_at": index_built_at,
        "bytes_checked": bytes_checked,
        "instance": default_source().slug(),
    }


def _ok(kind: str, data: dict, caveats: list[str], **prov) -> dict:
    return {"ok": True, "kind": kind, "data": data, "caveats": caveats,
            "provenance": _provenance(**prov)}


def _err(kind: str, message: str, *, hint: str = "", **extra) -> dict:
    """A failure the agent can act on. ``hint`` says what to do."""
    return {"ok": False, "kind": kind, "error": message, "hint": hint,
            "caveats": ["This call failed. Do not answer from memory or from the naming "
                        "convention; report the error and the hint."],
            "provenance": _provenance(), **extra}


def list_recipes(target: str | None = None) -> dict:
    """The catalogue. Needs no database."""
    from clinicalomx.extract import all_recipes, discover

    recipes = [r for r in all_recipes() if target is None or r.target == target]
    found = discover()
    caveats = []
    if not found:
        caveats.append(
            "No consumer distribution advertises recipes in this environment. That is not proof "
            "none exist - a consumer that is git-cloned rather than pip-installed advertises "
            "nothing.")
    return _ok("recipes", {"recipes": [r.to_dict() for r in recipes],
                           "consumer_providers": {k: len(v) for k, v in found.items()}}, caveats)


def find_roi(query: str, roi_type: str | None = None, limit: int = 10) -> dict:
    """Match free text against ROI names. Returns candidates; never disambiguates."""
    from clinicalomx.extract.select import find

    try:
        res = find(query, roi_type=roi_type, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a query failure is data for the agent, not a crash
        return _err("resolution", f"{type(exc).__name__}: {exc}",
                    hint="Check the instance is reachable, then retry with a simpler query.")
    caveats = res.caveats()
    if res.exact is None and not res.candidates:
        caveats.append(
            "Nothing matched. Do NOT infer an ROI name from the naming spine - a name that "
            "follows the convention is not evidence that the Record exists.")
    return _ok("resolution", res.to_dict(), caveats)


def describe_roi(uid_or_name: str, include_storage: bool = False) -> dict:
    """What this ROI can be turned into, and why not, where not."""
    from clinicalomx.extract.source import default_source

    src = default_source()
    roi = src.entity(uid=uid_or_name) or src.entity(name=uid_or_name, is_type=False)
    if roi is None:
        return _err("availability", f"no ROI Record with uid or name {uid_or_name!r}",
                    hint="Call find_roi first; do not guess a name from the convention.")

    av = describe(roi, include_storage=include_storage)
    return _ok("availability", av.to_dict(), av.caveats(),
               index_built_at=av.index_built_at, bytes_checked=av.bytes_checked)


def plan_extract(request: dict) -> dict:
    """Validate a request, resolve it, and return the plan and its token.

    A validation error is returned verbatim.
    """
    from pydantic import ValidationError

    from clinicalomx.extract.plan import plan
    from clinicalomx.extract.request import ExtractRequest

    try:
        parsed = ExtractRequest.model_validate(request)
    except ValidationError as exc:
        return _err("plan", f"the request is not valid: {exc}",
                    hint="Fix the named fields. Do not add fields the model rejects - "
                         "the schema is closed, and an unknown field is always a mistake.")
    try:
        result = plan(parsed)
    except (LookupError, ValueError) as exc:
        return _err("plan", str(exc),
                    hint="Ask the user to resolve the ambiguity; do not pick for them.")
    except Exception as exc:  # noqa: BLE001 - the agent surface must never raise
        return _err("plan", f"{type(exc).__name__}: {exc}",
                    hint="The instance may be unreachable from here. Report this verbatim; do "
                         "not describe what the plan would have been.")

    _PLANS[result.token] = result
    report, blocked = result.summarise()
    return _ok("plan", {**result.to_dict(), "report": report, "blocked": blocked},
               result.caveats(),
               index_built_at=result.index_built_at, bytes_checked=result.bytes_checked)


def assemble_extract(plan_token: str, confirm: bool = False) -> dict:
    """Build a plan that was already produced. Takes a token, never a request."""
    result = _PLANS.get(plan_token)
    if result is None:
        return _err("assembly", f"no plan with token {plan_token!r} in this session",
                    hint="Call plan_extract first and pass the token it returns. A plan cannot "
                         "be assembled unless it was planned and shown.")
    if not confirm:
        report, _ = result.summarise()
        return _err("assembly", "not confirmed",
                    hint="Show the report to the user and call again with confirm=true.",
                    data={"report": report, "token": plan_token})
    if result.is_stale():
        return _err("assembly", "this plan is stale; the instance may have changed since",
                    hint="Call plan_extract again and review the new plan.")
    try:
        built = assemble(result, confirm=True)
    except Exception as exc:  # noqa: BLE001 - a build failure is a result, not a crash
        return _err("assembly", f"{type(exc).__name__}: {exc}",
                    hint="Report this verbatim; do not claim the output exists.")
    return _ok("assembly", built, [],
               index_built_at=result.index_built_at, bytes_checked=result.bytes_checked)


# Corpus and datasets.
#: Jobs this process produced, by token. In-process only.
_JOBS: dict[str, Any] = {}


def survey_corpus(group_by: str | None = None, include_storage: bool = False) -> dict:
    """What exists across the whole instance, and what could be built from it."""
    try:
        sv = survey(group_by=group_by, include_storage=include_storage)
    except Exception as exc:  # noqa: BLE001 - the agent surface must never raise
        return _err("survey", f"{type(exc).__name__}: {exc}",
                    hint="The instance may be unreachable. Report this; do not estimate the "
                         "counts from anything you have already seen.")
    return _ok("survey", {**sv.to_dict(), "report": summarise(sv)}, sv.caveats(),
               index_built_at=sv.index_built_at, bytes_checked=sv.bytes_checked)


def search_rois(query: dict) -> dict:
    """ROIs matching a structured predicate: counts plus examples."""
    from pydantic import ValidationError

    from clinicalomx.extract.request import RoiQuery, ROISelector
    from clinicalomx.extract.select import select

    try:
        parsed = RoiQuery.model_validate(query)
    except ValidationError as exc:
        return _err("search", f"the query is not valid: {exc}",
                    hint="Fix the named fields. The schema is closed, so an unknown field is "
                         "always a mistake.")
    try:
        found = select(ROISelector(query=parsed))
    except Exception as exc:  # noqa: BLE001
        return _err("search", f"{type(exc).__name__}: {exc}",
                    hint="Report this verbatim rather than describing what might have matched.")

    caveats = []
    if len(found) >= parsed.limit:
        caveats.append(
            f"Exactly {parsed.limit} matched, which is the limit - there are probably more. "
            f"Raise `limit` deliberately or narrow the query; do not report this as the total.")
    return _ok("search", {
        "n": len(found),
        "examples": [{"uid": r.uid, "name": str(r.name)} for r in found[:20]],
        "truncated_examples": max(0, len(found) - 20),
        "query": parsed.model_dump(exclude_none=True),
    }, caveats)


def suggest_alternatives(request: dict) -> dict:
    """What would build for this ROI instead."""
    from pydantic import ValidationError

    from clinicalomx.extract.plan import nearest_buildable
    from clinicalomx.extract.request import ExtractRequest

    try:
        parsed = ExtractRequest.model_validate(request)
    except ValidationError as exc:
        return _err("alternatives", f"the request is not valid: {exc}",
                    hint="Fix the named fields before asking what else is possible.")
    try:
        alts = nearest_buildable(parsed)
    except Exception as exc:  # noqa: BLE001
        return _err("alternatives", f"{type(exc).__name__}: {exc}",
                    hint="Report this; do not offer alternatives you have not resolved.")
    return _ok("alternatives", {"alternatives": alts}, [
        "Only offer what is listed here. A recipe that is not in this list either does not exist "
        "or was not resolved, and either way it must not be suggested."])


def plan_dataset(request: dict, intent: str = "") -> dict:
    """Plan many ROIs under one intent; return the job token."""
    from pydantic import ValidationError

    from clinicalomx.extract.jobs import plan_dataset as build_job
    from clinicalomx.extract.request import ExtractRequest

    try:
        parsed = ExtractRequest.model_validate(request)
    except ValidationError as exc:
        return _err("job", f"the request is not valid: {exc}",
                    hint="Fix the named fields. Do not add fields the model rejects.")
    try:
        job = build_job(parsed, intent=intent)
    except (LookupError, ValueError) as exc:
        return _err("job", str(exc), hint="Narrow or correct the selector.")
    except Exception as exc:  # noqa: BLE001
        return _err("job", f"{type(exc).__name__}: {exc}",
                    hint="Report this verbatim; do not describe the job it would have been.")

    _JOBS[job.token] = job
    report, nothing_to_build = job.summarise()
    return _ok("job", {**job.to_dict(), "report": report,
                       "nothing_to_build": nothing_to_build},
               job_caveats(job), index_built_at=job.index_built_at)


def job_caveats(job) -> list[str]:
    """The caveats a job must be relayed with, cost first."""
    est = job.estimate()
    out = [f"{est['note']} Say this to the user BEFORE they agree, not after."]
    if job.unbuildable:
        out.append(
            f"{len(job.unbuildable)} of {len(job.plans)} ROI(s) produce nothing and will be "
            f"skipped: {', '.join(job.unbuildable[:5])}. Report the shortfall; a job that "
            f"quietly covered fewer ROIs than asked reads as complete.")
    out += list(job.warnings)
    return out


def job_status(job_token: str) -> dict:
    """Per-unit progress for a submitted job."""
    job = _JOBS.get(job_token)
    if job is None:
        return _err("job_status", f"no job with token {job_token!r} in this session",
                    hint="Call plan_dataset first and keep the token it returns.")
    tally: dict[str, int] = {}
    for state in job.status.values():
        tally[state] = tally.get(state, 0) + 1
    report, _ = job.summarise()
    return _ok("job_status", {"token": job.token, "tally": tally,
                              "status": dict(job.status), "report": report}, [],
               index_built_at=job.index_built_at)


def assemble_job(job_token: str, confirm: bool = False) -> dict:
    """Build a job that was already planned. Takes a token, never a request."""
    job = _JOBS.get(job_token)
    if job is None:
        return _err("job_assembly", f"no job with token {job_token!r} in this session",
                    hint="Call plan_dataset first and show its report. A job cannot be built "
                         "unless it was planned and seen.")
    if not confirm:
        report, _ = job.summarise()
        return _err("job_assembly", "not confirmed",
                    hint="Show the report AND the cost to the user, then call again with "
                         "confirm=true.",
                    data={"report": report, "token": job_token,
                          "estimate": job.estimate()})
    try:
        out = run_job(job, confirm=True)
    except Exception as exc:  # noqa: BLE001 - a build failure is a result, not a crash
        return _err("job_assembly", f"{type(exc).__name__}: {exc}",
                    hint="Report this verbatim; do not claim the outputs exist.")
    return _ok("job_assembly", out,
               [f"{len(out['failed'])} unit(s) failed; name them rather than reporting a count."]
               if out.get("failed") else [],
               index_built_at=job.index_built_at)


def forget_plans() -> dict:
    """Drop the in-process plan and job stores."""
    n = len(_PLANS) + len(_JOBS)
    _PLANS.clear()
    _JOBS.clear()
    return _ok("forget", {"dropped": n}, [])
