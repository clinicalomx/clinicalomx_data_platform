"""Submitting an approved plan to an orchestrator, so ``assemble`` runs where the bytes are.

:func:`payload_for` turns a :class:`~clinicalomx.extract.plan.Plan` into JSON and :func:`rehydrate`
turns it back, refusing a payload whose token or recipe content does not match. The submitter is
injected; :func:`use_prefect` wires Prefect. Prefect is never imported at module load.
Resubmitting the same plan resumes it: chunking is deterministic and built outputs are skipped.
"""
from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from clinicalomx.extract import all_recipes
from clinicalomx.extract.jobs import CORE_BUDGET_MINUTES, pack
from clinicalomx.extract.plan import Plan, Unit

#: Version of the payload's wire form. A worker refuses an unknown one.
WIRE_VERSION = 1

#: Prefect tag prefix carrying the plan token, shared by every run of one plan.
TAG_PREFIX = "extract-plan:"

#: The flow and deployment the chunks run on.
FLOW_NAME = "extract-assemble"
DEPLOYMENT = f"{FLOW_NAME}/pipeline-extract-assemble"


class SubmitError(RuntimeError):
    """A plan could not be handed to the orchestrator, or could not be trusted on arrival."""


@dataclass(frozen=True)
class Chunk:
    """One run's worth of an approved plan: the payload, plus which of its units to build."""

    token: str
    outputs: tuple[str, ...]
    payload: dict

    @property
    def tag(self) -> str:
        return TAG_PREFIX + self.token


@dataclass(frozen=True)
class Run:
    """A submitted run as the orchestrator reports it."""

    id: str
    name: str
    state: str
    token: str = ""
    submitted_at: str = ""
    outputs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "token": self.token,
                "submitted_at": self.submitted_at, "outputs": list(self.outputs)}


# Wire form.
def payload_for(plan) -> dict:
    """The approved plan as JSON, carrying exactly what ``token`` and ``assemble`` read.

    Includes ``recipe_content``; excludes the availability tree.
    """
    return {
        "wire_version": WIRE_VERSION,
        "clinicalomx_version": _version(),
        "token": plan.token,
        "recipe": plan.recipe,
        "recipe_content": plan.recipe_content,
        "target": plan.target,
        "layout": plan.layout,
        "roi_name": plan.roi_name,
        "units": [u.to_dict() for u in plan.units],
        "blocked": list(plan.blocked),
        "warnings": list(plan.warnings),
        "governance": dict(plan.governance),
        "placement": {k: v.to_dict() if hasattr(v, "to_dict") else v
                      for k, v in plan.placement.items()},
        "request": plan.request,
        "index_built_at": plan.index_built_at,
        "bytes_checked": plan.bytes_checked,
        # The only part of `Availability` read after planning.
        "artifact_keys": sorted(plan.artifact_keys),
    }


@dataclass(frozen=True)
class _CarriedKeys:
    """Stands in for ``Plan.availability`` on a rehydrated plan. Holds the artifact keys only."""

    keys: tuple[str, ...]

    @property
    def recipes(self):
        return [_CarriedKeys(self.keys)]

    @property
    def elements(self):
        return [_CarriedKeys(self.keys)]


def rehydrate(payload: dict):
    """The payload back into the approved ``Plan``, or ``SubmitError`` naming the failed check.

    Checks the wire version, the worker's recipe content, and the token, in that order.
    """
    got = payload.get("wire_version")
    if got != WIRE_VERSION:
        raise SubmitError(
            f"this payload is wire version {got!r} and this environment reads {WIRE_VERSION}. "
            f"The submitting app and this worker are on different clinicalomx releases; do not "
            f"guess at the difference, ship the matching one.")

    plan = Plan(
        recipe=payload["recipe"],
        target=payload["target"],
        layout=payload["layout"],
        roi_name=payload["roi_name"],
        availability=_CarriedKeys(tuple(payload.get("artifact_keys") or ())),
        units=[Unit(roi_uid=u["roi_uid"], roi_name=u["roi_name"], output=u["output"],
                    elements=dict(u["elements"]),
                    child_elements={k: dict(v) for k, v in (u.get("child_elements") or {}).items()},
                    omitted=tuple(u.get("omitted") or ()),
                    verdict=u["verdict"], reason=u.get("reason", ""))
               for u in payload["units"]],
        blocked=list(payload.get("blocked") or []),
        warnings=list(payload.get("warnings") or []),
        governance=dict(payload.get("governance") or {}),
        placement=dict(payload.get("placement") or {}),
        request=payload.get("request"),
        index_built_at=payload.get("index_built_at", ""),
        bytes_checked=bool(payload.get("bytes_checked")),
        recipe_content=payload.get("recipe_content") or {},
    )

    here = next((r for r in all_recipes() if r.name == plan.recipe), None)
    if here is None:
        raise SubmitError(
            f"no recipe named {plan.recipe!r} resolves here. The plan was built where it does, so "
            f"this environment is missing the consumer distribution that advertises it.")
    if _canon(here.to_dict()) != _canon(plan.recipe_content):
        raise SubmitError(
            f"recipe {plan.recipe!r} here is not the recipe this plan was built from. `assemble` "
            f"looks a recipe up by NAME, so building would run a different computation under the "
            f"same token. Re-plan against this environment.")

    if plan.token != payload["token"]:
        raise SubmitError(
            f"this payload does not hash to the token it carries ({payload['token']} declared, "
            f"{plan.token} computed). Either it was edited in flight, or the submitting app and "
            f"this worker disagree about what a token covers. Nothing is built on a maybe.")
    return plan


def _canon(obj) -> str:
    """JSON with tuples and lists spelled the same."""
    return json.dumps(obj, sort_keys=True, default=str)


def _version() -> str:
    from clinicalomx import __version__

    return __version__


# Chunking.
def units_per_run(*, walltime_minutes: int) -> int:
    """Per-core budgets per run: ``walltime // jobs.CORE_BUDGET_MINUTES``, at least 1."""
    return max(1, int(walltime_minutes) // int(CORE_BUDGET_MINUTES))


def chunks_for(plan, *, per_run: int) -> list[Chunk]:
    """The approved plan split into runs. Contiguous, in plan order, and nothing is dropped.

    ``per_run`` counts core budgets; a merged unit weighs as many as its cores.
    """
    payload = payload_for(plan)
    if not plan.buildable:
        raise SubmitError(
            "this plan would build nothing"
            + (f"; it is blocked on {', '.join(plan.blocked)}" if plan.blocked else ""))
    return [Chunk(token=plan.token, outputs=tuple(u.output for u in run), payload=payload)
            for run in pack(plan.buildable, per_run)]


def restrict(plan, outputs):
    """A copy of ``plan`` holding only the named units, for a worker building one chunk.

    The copy has its own token; report both.
    """
    wanted = list(outputs)
    unknown = sorted(set(wanted) - {u.output for u in plan.units})
    if unknown:
        raise SubmitError(
            f"{len(unknown)} output(s) are not in this plan: {unknown[:3]}. A chunk may only name "
            f"units the approved plan already carries.")
    keep = [u for u in plan.units if u.output in set(wanted)]
    return dataclasses.replace(plan, units=keep)


# The injected orchestrator.
_SUBMITTER: Callable[..., Any] | None = None
_RUN_READER: Callable[..., Any] | None = None
_SIZER: Callable[..., Any] | None = None


def register_submitter(fn: Callable[..., Any]) -> None:
    """Register what :func:`submit` hands a :class:`Chunk` to.

    Signature: ``submit(chunk: Chunk) -> Run``.
    """
    global _SUBMITTER
    _SUBMITTER = fn


def register_run_reader(fn: Callable[..., Any]) -> None:
    """Register what :func:`runs` asks for submitted runs.

    Signature: ``read(token: str | None, limit: int) -> list[Run]``.
    """
    global _RUN_READER
    _RUN_READER = fn


def register_sizer(fn: Callable[..., Any] | None) -> None:
    """Register what tells :func:`submit` how long one run may take.

    Signature: ``walltime_minutes() -> int``.
    """
    global _SIZER
    _SIZER = fn


def use_prefect(*, deployment: str = DEPLOYMENT) -> None:
    """Wire the submitter, run reader and sizer to Prefect. Needs ``clinicalomx[prefect]``.

    Call once at startup::

        from clinicalomx.extract.submit import use_prefect
        use_prefect()
    """
    register_submitter(lambda chunk: prefect_submitter(chunk, deployment=deployment))
    register_run_reader(prefect_run_reader)
    register_sizer(lambda: prefect_walltime_minutes(deployment=deployment))


def submit(plan, *, per_run: int | None = None) -> list[Run]:
    """Hand every chunk of an approved plan to the orchestrator. Returns the submitted runs."""
    if _SUBMITTER is None:
        raise SubmitError(
            "no submitter is registered, so this app cannot start a build. Call `use_prefect()` "
            "at startup, or register your own submitter.")
    size = per_run if per_run is not None else units_per_run(walltime_minutes=walltime_minutes())
    return [_SUBMITTER(chunk) for chunk in chunks_for(plan, per_run=size)]


def walltime_minutes() -> int:
    """How long one run may take, from the injected orchestrator."""
    if _SIZER is None:
        raise SubmitError(
            "no sizer is registered, so how many units fit one run cannot be derived. Call "
            "`use_prefect()` at startup, or pass `per_run` explicitly.")
    return int(_SIZER())


def can_submit() -> bool:
    """Whether this process has an orchestrator to submit to."""
    return _SUBMITTER is not None


def runs(token: str | None = None, *, limit: int = 50) -> list[Run]:
    """Submitted runs and their states, newest first. Empty when nothing is registered."""
    if _RUN_READER is None:
        return []
    return list(_RUN_READER(token, limit))


# Prefect implementations, opt-in.
def prefect_submitter(chunk: Chunk, *, deployment: str = DEPLOYMENT) -> Run:
    """Create one independent flow run per chunk. Needs ``clinicalomx[prefect]``.

    No ``idempotency_key``: resubmitting a plan must start a live run to resume a killed build.
    """
    with _client() as c:
        dep = _read_deployment(c, deployment)
        run = c.create_flow_run_from_deployment(
            dep.id,
            parameters={"payload": chunk.payload, "outputs": list(chunk.outputs)},
            tags=[chunk.tag],
        )
    return Run(id=str(run.id), name=str(run.name), state=_state_of(run), token=chunk.token,
               submitted_at=str(getattr(run, "created", "") or ""), outputs=chunk.outputs)


def prefect_walltime_minutes(*, deployment: str = DEPLOYMENT) -> int:
    """The walltime the deployment declares, in minutes. Needs ``clinicalomx[prefect]``.

    Raises :class:`SubmitError` when the deployment declares none.
    """
    with _client() as c:
        dep = _read_deployment(c, deployment)
    raw = (getattr(dep, "job_variables", None) or {}).get("walltime")
    minutes = _walltime_minutes(raw)
    if minutes is None:
        raise SubmitError(
            f"deployment {deployment!r} declares no usable walltime (job_variables.walltime is "
            f"{raw!r}), so there is nothing to size a chunk against. Fix the deployment rather "
            f"than submitting a chunk that cannot fit in its job.")
    return minutes


def _walltime_minutes(raw) -> int | None:
    """SLURM's ``[D-]H:MM:SS`` to whole minutes, or None when it is not that."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text, days = raw.strip(), 0
    if "-" in text:
        head, _, text = text.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = text.split(":")
    if not (1 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
        return None
    hours, minutes, seconds = (list(map(int, parts)) + [0, 0])[:3]
    if len(parts) == 1:  # SLURM reads a bare number as MINUTES, not hours.
        hours, minutes = 0, int(parts[0])
    return days * 24 * 60 + hours * 60 + minutes + seconds // 60


def prefect_run_reader(token: str | None = None, limit: int = 50) -> list[Run]:
    """Submitted runs, newest first, by flow name, or by plan tag when ``token`` is given."""
    from prefect.client.schemas.filters import (
        FlowFilter,
        FlowFilterName,
        FlowRunFilter,
        FlowRunFilterTags,
    )
    from prefect.client.schemas.sorting import FlowRunSort

    with _client() as c:
        found = c.read_flow_runs(
            flow_filter=FlowFilter(name=FlowFilterName(any_=[FLOW_NAME])),
            flow_run_filter=(FlowRunFilter(tags=FlowRunFilterTags(all_=[TAG_PREFIX + token]))
                             if token else None),
            sort=FlowRunSort.EXPECTED_START_TIME_DESC,
            limit=limit)
    return [_run_of(r) for r in found]


def _read_deployment(client, deployment: str):
    """The deployment, or a :class:`SubmitError` naming the server or the deployment."""
    try:
        return client.read_deployment_by_name(deployment)
    except Exception as exc:
        raise SubmitError(_read_failure(client, deployment, exc)) from exc


def _read_failure(client, deployment: str, exc: Exception) -> str:
    """Whether a failed read was an unreachable server or a missing deployment."""
    api = os.environ.get("PREFECT_API_URL") or ""
    where = (f"PREFECT_API_URL is {api}" if api else
             "PREFECT_API_URL is not set in this process, so the client fell back to its default "
             "address; a container serving this app needs it pointed at the Prefect service")
    detail = f"[{type(exc).__name__}: {exc}]"
    if _server_answers(client):
        return (f"the Prefect server answers, but deployment {deployment!r} is not registered on "
                f"it. Deploy it before submitting. {where}. {detail}")
    return (f"the Prefect server cannot be reached from this process, so deployment "
            f"{deployment!r} could not be read. {where}. {detail}")


def _server_answers(client) -> bool:
    """Whether the API answers a healthcheck. Never raises."""
    try:
        return client.api_healthcheck() is None
    except Exception:  # noqa: BLE001 - any failure here IS the unreachable answer
        return False


def _client():
    try:
        from prefect.client.orchestration import get_client
    except ImportError as exc:  # pragma: no cover
        raise SubmitError(
            "submitting needs Prefect: install clinicalomx[prefect] (or pip install prefect)"
        ) from exc
    return get_client(sync_client=True)


def _state_of(run) -> str:
    state = getattr(run, "state", None)
    return str(getattr(state, "name", "") or getattr(state, "type", "") or "UNKNOWN")


def _run_of(run) -> Run:
    tags = list(getattr(run, "tags", ()) or ())
    token = next((t[len(TAG_PREFIX):] for t in tags if t.startswith(TAG_PREFIX)), "")
    outputs = tuple((getattr(run, "parameters", None) or {}).get("outputs") or ())
    return Run(id=str(run.id), name=str(run.name), state=_state_of(run), token=token,
               submitted_at=str(getattr(run, "created", "") or ""), outputs=outputs)
