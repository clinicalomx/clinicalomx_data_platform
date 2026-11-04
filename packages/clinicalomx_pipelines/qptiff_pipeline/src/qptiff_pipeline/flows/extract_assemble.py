"""extract-assemble: build an approved extraction plan on the HPC, where the bytes are.

The Build app cannot read full-resolution pixels, so its Assemble button submits this flow. The
parameter is the plan itself, not the request. ``rehydrate`` refuses an unknown wire version, a
recipe that resolves differently here, or a payload that does not match its token.

One run per chunk. The output key is the ledger: re-submitting the same payload rebuilds only what
is missing. A soft time budget stops before a unit that cannot finish and reports it deferred. A run
that built nothing and failed something is Failed and is not retried. Plan staleness is reported,
not enforced, because SLURM queue time would make every plan stale.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

STAGE = "extract"


def _age_minutes(index_built_at: str) -> float:
    """How many minutes ago the plan's index was built. Reported, never enforced."""
    from datetime import UTC, datetime

    if not index_built_at:
        return -1.0
    try:
        built = datetime.strptime(index_built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return -1.0
    return round((datetime.now(UTC) - built).total_seconds() / 60, 1)


# retries=1: a retry runs inside the same SLURM allocation. `materialize` skips outputs already
# written, so it is nearly free.
@flow(name="extract-assemble", log_prints=True, retries=1, retry_delay_seconds=60)
def extract_assemble(
    payload: dict,
    outputs: list[str] | None = None,
    max_runtime_minutes: int = 300,
) -> dict:
    """Build the units of an approved plan. Takes the plan, never a request.

    payload: the wire form from ``clinicalomx.extract.submit.payload_for``: the plan, the recipe and
        the token that names both.
    outputs: which of the plan's units this run builds. Omitted means all of them.
    max_runtime_minutes: soft budget, under the deployment's walltime. The flow stops before a unit
    it
        cannot finish, reports the rest deferred and exits Completed. Re-submit to continue.
    """
    import time

    logger = get_run_logger()
    started = time.monotonic()

    import lamindb as ln
    from clinicalomx.extract.assemble import assemble
    from clinicalomx.extract.submit import rehydrate, restrict
    from clinicalomx.snapshot import Snapshot

    from qptiff_pipeline.domain import governance as declared
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    # Load the governance table: the SIF runs a git clone with no entry point. Idempotent.
    declared.register()

    track_prefect_run(STAGE)
    try:
        plan = rehydrate(payload)
        part = restrict(plan, outputs) if outputs else plan
        units = part.buildable
        age = _age_minutes(plan.index_built_at)
        logger.info(
            f"extract-assemble {plan.recipe} -> {plan.target} for {plan.roi_name}: "
            f"{len(units)} of {len(plan.buildable)} unit(s), plan token {plan.token}, "
            f"chunk token {part.token}, plan index built {plan.index_built_at} ({age} min ago)")
        for line in part.summarise()[0].splitlines():
            logger.info(f"  {line}")

        if not units:
            raise ValueError(
                f"this chunk names no buildable unit of plan {plan.token}. `outputs` must name "
                f"units the approved plan carries and did not skip.")

        # One Snapshot for the whole chunk.
        snap = Snapshot()
        built, failed, deferred, registered = [], [], [], 0

        # One unit at a time, so the soft budget has somewhere to stop.
        for i, unit in enumerate(units):
            elapsed = (time.monotonic() - started) / 60
            if elapsed >= max_runtime_minutes:
                deferred = [u.output for u in units[i:]]
                logger.warning(
                    f"soft budget of {max_runtime_minutes} min reached after {elapsed:.0f} min; "
                    f"deferring {len(deferred)} unit(s). Re-submit this payload to finish them - "
                    f"nothing already built will be rebuilt.")
                break
            one = restrict(part, [unit.output])
            try:
                # force_stale: staleness is waived and reported above.
                out = assemble(one, confirm=True, force_stale=True, snapshot=snap)
            except Exception as exc:  # one bad unit must not lose the rest of the chunk
                logger.error(f"{unit.roi_name}: {type(exc).__name__}: {exc}")
                failed.append(f"{unit.roi_name}: {type(exc).__name__}: {exc}")
                continue
            built += out.get("built", [])
            failed += out.get("failed", [])
            registered += int(out.get("n_registered", 0))
            logger.info(f"{unit.roi_name}: built {out.get('n_built', 0)}, "
                        f"registered {out.get('n_registered', 0)} -> {unit.output}")

        result = {
            "plan_token": plan.token,
            "chunk_token": part.token,
            "recipe": plan.recipe,
            "roi_name": plan.roi_name,
            "plan_index_built_at": plan.index_built_at,
            "plan_age_minutes": age,
            "n_units": len(units),
            "n_built": len(built),
            "n_registered": registered,
            "built": built,
            # Instances, never a bare count.
            "failed": failed,
            "deferred": deferred,
        }
        _publish(plan, part, result)
        logger.info(f"extract-assemble done: {len(built)} built, {registered} registered, "
                    f"{len(failed)} failed, {len(deferred)} deferred")
        _refuse_if_nothing_built(result)
        return result
    finally:
        ln.finish()


def _refuse_if_nothing_built(result: dict) -> None:
    """Fail a run that built nothing and failed something, after `_publish` has recorded it.

    A partial chunk stays Completed with its failures listed. A chunk whose units were all
    deferred failed nothing.
    """
    if result["n_built"] or not result["failed"]:
        return
    _exhaust_retries()
    raise RuntimeError(
        f"extract-assemble built nothing for {result['recipe']} on {result['roi_name']}: "
        f"{len(result['failed'])} unit(s) failed - " + "; ".join(result["failed"]))


def _exhaust_retries() -> None:
    """Set this flow run's retry budget to zero on the server, so its next Failed state is final."""
    from prefect.context import FlowRunContext

    ctx = FlowRunContext.get()
    if ctx is None or ctx.flow_run is None:
        return
    from prefect import get_client

    policy = ctx.flow_run.empirical_policy.model_copy(update={"retries": 0})
    with get_client(sync_client=True) as client:
        client.update_flow_run(flow_run_id=ctx.flow_run.id, empirical_policy=policy)


def _publish(plan, part, result: dict) -> None:
    """Publish one table artifact per run, keyed on the plan token, listing what the chunk built."""
    rows = [{"roi_name": b.get("roi_name", ""), "output": b.get("output", ""),
             "registered": b.get("registered", False), "state": "built",
             "detail": b.get("detail", "")} for b in result["built"]]
    # The whole string as the detail. ROI names contain colons, so it is not split.
    rows += [{"roi_name": "", "output": "", "registered": False,
              "state": "failed", "detail": f} for f in result["failed"]]
    rows += [{"roi_name": "", "output": o, "registered": False, "state": "deferred",
              "detail": "soft time budget"} for o in result["deferred"]]
    if not rows:
        return
    try:
        from prefect.artifacts import create_table_artifact

        create_table_artifact(
            key=f"extract-{plan.token}",
            table=rows,
            description=(f"{plan.recipe} -> {plan.target} for {plan.roi_name}; chunk "
                         f"{part.token} of plan {plan.token}: {result['n_built']} built, "
                         f"{len(result['failed'])} failed, {len(result['deferred'])} deferred"),
        )
    except Exception as exc:  # never lose a build to a reporting failure
        print(f"table artifact publish failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
