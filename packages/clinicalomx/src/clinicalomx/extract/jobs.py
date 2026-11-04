"""A :class:`Job`: one intent, one Plan per matched ROI, and a token over all of them.

Resume is ``idempotency.materialize``, which skips outputs whose bytes exist. :meth:`Job.estimate`
states the unit count and a rough cost before anything runs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from clinicalomx.extract.assemble import AssemblyError, assemble
from clinicalomx.extract.layout import _human as human
from clinicalomx.extract.plan import STALE_AFTER
from clinicalomx.snapshot import Snapshot

#: The pipeline's per-core memory budget in GB, from prefect.yaml. A stated envelope, not a
#: measurement.
CORE_BUDGET_GB = 64
CORE_BUDGET_MINUTES = 90


def unit_cores(unit) -> int:
    """How many per-core budgets one unit costs: every child of a merged unit, else one."""
    return len(unit.child_elements) or 1


def pack(units, per_run: int) -> list[list]:
    """``units`` in contiguous, ordered runs of at most ``per_run`` cores. Nothing is dropped.

    Weighted by :func:`unit_cores`; a merged unit larger than a run rides alone.
    """
    size = max(1, int(per_run))
    runs: list[list] = []
    used = size
    for unit in units:
        cost = unit_cores(unit)
        if used + cost > size:
            runs.append([])
            used = 0
        runs[-1].append(unit)
        used += cost
    return runs


def _scratch(plans, layouts: dict) -> tuple[int | None, str]:
    """``(uncompressed output bytes over every plan, basis)``, from each plan's measured layout.

    Per-child plans scale by output count. A missing layout or measurement makes the total ``None``.
    """
    measure = ("run `layout.layout_of(plan)` - the Build app's layout panel - somewhere that "
               "has `clinicalomx[extract]` and can read the stores")
    if not layouts:
        return None, f"this estimate was given no measured layout; {measure}"
    total = 0
    for plan in plans:
        if not plan.buildable:
            continue
        layout = layouts.get(plan.token)
        if layout is None:
            return None, f"no layout was given for {plan.roi_name}; {measure}"
        if not layout.measured:
            if any(n.startswith("no layout is known for target") for n in layout.notes):
                return None, (f"the layout preview has no renderer for {layout.target} "
                              f"({layout.recipe}), so it cannot size {plan.roi_name} at all; "
                              f"that renderer is what is missing")
            return None, (f"the layout for {plan.roi_name} was read without store metadata, "
                          f"which is what a host without `clinicalomx[extract]` or without "
                          f"storage access produces; {measure}")
        sizes: dict[str, list[int]] = {}
        for piece in layout.pieces:
            if piece.measured:
                sizes.setdefault(piece.element, []).append(piece.stored_bytes or piece.size_bytes)
        one = 0
        for piece in layout.pieces:
            if piece.measured:
                one += piece.stored_bytes or piece.size_bytes
            elif sizes.get(piece.element):
                one += sum(sizes[piece.element]) // len(sizes[piece.element])
            elif piece.slot in ("series", "images", "labels"):
                return None, (f"{piece.element} in {plan.roi_name} was not measured"
                              + (f": {piece.note}" if piece.note else ""))
        total += one * (1 if plan.layout == "merged" else layout.n_outputs)
    return total, "extrapolated from one representative ROI per plan"


def _human(n: int) -> str:
    return human(n)


@dataclass
class Job:
    """One intent, many plans."""

    #: The user's ask, verbatim.
    intent: str
    request: dict
    plans: list
    #: ``{output key: pending | built | failed | skipped}``.
    status: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    index_built_at: str = ""

    def __post_init__(self) -> None:
        for unit in self.units:
            self.status.setdefault(unit.output, "pending")

    @property
    def units(self) -> list:
        return [u for p in self.plans for u in p.buildable]

    @property
    def blocked(self) -> list[str]:
        """Elements that blocked at least one member. Informational; an empty job is the gate."""
        return sorted({b for p in self.plans for b in p.blocked})

    @property
    def unbuildable(self) -> list[str]:
        """ROIs that produced no unit."""
        return sorted(str(p.roi_name) for p in self.plans if not p.buildable)

    @property
    def token(self) -> str:
        """Content hash over the intent and every member plan."""
        blob = json.dumps({"intent": self.intent,
                           "plans": sorted(p.token for p in self.plans),
                           "index_built_at": self.index_built_at},
                          sort_keys=True)
        return "job_" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    def is_stale(self, *, now: str | None = None, max_age: int | None = None) -> bool:
        age = STALE_AFTER if max_age is None else max_age
        return any(p.is_stale(now=now, max_age=age) for p in self.plans)

    def estimate(self, *, per_run: int | None = None, layouts: dict | None = None) -> dict:
        """Unit count and a rough footprint, stated before anybody agrees. Coarse.

        A merged unit costs one budget per core under its anchor. With ``per_run``, reports the
        number of runs :func:`pack` makes and any output too large for one run. Scratch is a number
        from ``layouts`` (``{plan token: layout.TargetLayout}``), or the reason there is none.
        """
        n = len(self.units)
        # One crop per element per child for a merged unit; one per element otherwise.
        arrays = sum(sum(len(e) for e in u.child_elements.values()) or len(u.elements)
                     for u in self.units)
        cores = sum(unit_cores(u) for u in self.units)
        note = (f"The pipeline budgets {CORE_BUDGET_GB} GB of RAM and {CORE_BUDGET_MINUTES} "
                f"minutes for ONE core's full-resolution field of view. This is {cores} core(s) "
                f"in {n} output(s)" + (", which is not a laptop operation." if cores > 1 else "."))
        if cores > n:
            note += (f" A merged output reads every core under its anchor, so the time is {cores} "
                     f"cores' worth and not {n}.")
        out = {"units": n, "cores": cores, "arrays": arrays,
               "per_core_budget_gb": CORE_BUDGET_GB,
               "rough_hours": round(cores * CORE_BUDGET_MINUTES / 60, 1)}
        if per_run is not None:
            size = max(1, int(per_run))
            over = [u for u in self.units if unit_cores(u) > size]
            out["runs"] = len(pack(self.units, size))
            out["over_run_budget"] = [u.output for u in over]
            if over:
                worst = max(unit_cores(u) for u in over)
                note += (f" {len(over)} output(s) read more cores than one run's {size}-core "
                         f"budget, up to {worst} (~{round(worst * CORE_BUDGET_MINUTES / 60, 1)}h "
                         f"against a {round(size * CORE_BUDGET_MINUTES / 60, 1)}h run). A merged "
                         f"output cannot be split, so each runs alone and by this envelope may "
                         f"not finish inside the walltime.")
        scratch, why = _scratch(self.plans, layouts or {})
        out["scratch_bytes"] = scratch
        out["scratch_basis"] = why
        note += " The GB figure is RAM. " + (
            f"Scratch: {_human(scratch)} uncompressed ({why})." if scratch is not None
            else f"Scratch is NOT measured: {why}.")
        out["note"] = note
        return out

    def to_dict(self) -> dict:
        return {"token": self.token, "intent": self.intent, "request": self.request,
                "n_plans": len(self.plans), "n_units": len(self.units),
                "blocked": self.blocked, "unbuildable": self.unbuildable,
                "status": dict(self.status),
                "estimate": self.estimate(), "warnings": list(self.warnings),
                "index_built_at": self.index_built_at,
                "plans": [p.to_dict() for p in self.plans]}

    def summarise(self, *, max_examples: int = 5) -> tuple[str, bool]:
        est = self.estimate()
        done = sum(1 for v in self.status.values() if v == "built")
        lines = [
            f"job      {self.token}",
            f"intent   {self.intent}",
            f"plans    {len(self.plans)} ROI(s) -> {len(self.units)} unit(s), {done} built",
            f"cost     ~{est['rough_hours']}h over {est['cores']} core(s), up to "
            f"{est['per_core_budget_gb']} GB of RAM per core",
            "scratch  " + (f"{_human(est['scratch_bytes'])} uncompressed ({est['scratch_basis']})"
                           if est["scratch_bytes"] is not None
                           else f"NOT MEASURED - {est['scratch_basis']}"),
            f"index    {self.index_built_at}",
        ]
        if self.unbuildable:
            shown = ", ".join(self.unbuildable[:3])
            more = f" and {len(self.unbuildable) - 3} more" if len(self.unbuildable) > 3 else ""
            lines += ["", f"{len(self.unbuildable)} ROI(s) produce nothing ({shown}{more})",
                      f"  because: {', '.join(self.blocked)}"]
        if not self.units:
            lines += ["", "NOTHING TO BUILD - every member is blocked."]
        lines += [""]
        for unit in self.units[:max_examples]:
            lines.append(f"  {self.status.get(unit.output, 'pending'):8} {unit.roi_name}")
        if len(self.units) > max_examples:
            lines.append(f"  ... and {len(self.units) - max_examples} more")
        for w in self.warnings:
            lines.append(f"  WARNING  {w}")
        return "\n".join(lines), not self.units


def plan_dataset(request, *, intent: str = "", snapshot=None, now: str | None = None,
                 limit: int = 500, source=None) -> Job:
    """One :class:`Job` from a request whose selector may match many ROIs."""
    from clinicalomx.extract.plan import plan as plan_one
    from clinicalomx.extract.select import select

    snap = snapshot if snapshot is not None else Snapshot()
    roots = select(request.roi, snapshot=snap, source=source)
    if not roots:
        raise LookupError("the ROI selector matched nothing")

    warnings: list[str] = []
    if len(roots) > limit:
        # Report what the limit dropped.
        warnings.append(
            f"the selector matched {len(roots)} ROIs; planning the first {limit}. Narrow the "
            f"selector, or raise the limit deliberately.")
        roots = roots[:limit]

    plans, failed = [], []
    for roi in roots:
        # Pin each member to one ROI by uid.
        one = request.model_copy(update={"roi": request.roi.model_copy(
            update={"uid": roi.uid, "name": None, "fuzzy": None, "query": None,
                    "include": "self", "child_type": None})})
        try:
            plans.append(plan_one(one, snapshot=snap, now=now, source=source))
        except (LookupError, ValueError) as exc:
            # Keep the reason and plan the rest.
            failed.append(f"{roi.name}: {exc}")
    warnings += failed

    return Job(intent=intent or f"{request.target} from {len(roots)} ROI(s)",
               request=request.model_dump(exclude_none=True), plans=plans,
               warnings=warnings,
               index_built_at=plans[0].index_built_at if plans else (now or ""))


def assemble_job(job: Job, *, confirm: bool = False, force_stale: bool = False,
                 snapshot=None, **builder_kw) -> dict:
    """Build every plan in a job. Resumable. Per-plan failures are collected, not raised."""
    if not confirm:
        raise AssemblyError("assemble_job needs confirm=True; a job is meant to be read first")
    if not job.units:
        raise AssemblyError(
            f"this job would build nothing: every one of its {len(job.plans)} ROI(s) is blocked"
            + (f" on {', '.join(job.blocked)}" if job.blocked else ""))
    if job.is_stale() and not force_stale:
        raise AssemblyError(
            f"this job's index was built at {job.index_built_at} and may no longer describe the "
            f"instance. Re-plan, or pass force_stale=True if you accept the risk.")

    built, failed = [], []
    for one in job.plans:
        try:
            out = assemble(one, confirm=True, force_stale=True, snapshot=snapshot, **builder_kw)
        except Exception as exc:  # noqa: BLE001 - one bad plan must not lose the rest of the job
            for unit in one.buildable:
                job.status[unit.output] = "failed"
            failed.append(f"{one.roi_name}: {type(exc).__name__}: {exc}")
            continue
        for item in out.get("built", []):
            job.status[item["output"]] = "built"
        built += out.get("built", [])
        failed += out.get("failed", [])

    return {"token": job.token, "n_built": len(built), "built": built,
            "failed": failed, "status": dict(job.status)}
