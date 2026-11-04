"""clear-bgnorm-failures: clear per-core bgnorm failure markers, so failed cores run again.

Targets cores with no ``bgnormed`` artifact that were already attempted (``bgnorm_core_failed`` or
``bgnorm_attempts`` > 0). Clears both markers and re-queues the TMA to ``bgnorm_queued``. Deletes
nothing. A broken core fails again, bounded by the runner's ``max_resubmit``.

Re-queues only from ``bgnorm_failed`` or ``bgnorm_pending``. A ``complete`` TMA is left alone unless
``include_complete=True``. A TMA upstream of bgnorm has its markers cleared but its state untouched.

``dry_run=True`` (default) only logs the plan. Metadata only, so it runs on the VM.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="clear-bgnorm-failures", log_prints=True, retries=1, retry_delay_seconds=60)
def clear_bgnorm_failures(
    dry_run: bool = True,
    tma_uids: list[str] | None = None,
    include_complete: bool = False,
    limit: int | None = None,
) -> dict:
    """Clear bgnorm_core_failed and bgnorm_attempts on un-bgnormed cores, and re-queue their TMAs.

    tma_uids: restrict the scan to these TMAs (default None = every Core record).
    include_complete: also re-queue TMAs a reviewer accepted (``complete``). Default False.
    limit: cap on cores cleared this run (None = all).
    """
    logger = get_run_logger()

    import lamindb as ln
    from clinicalomx import get_state, set_state

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("clear-bgnorm-failures")
    try:
        core_type = get_or_create_type(states.CORE_TYPE)
        tma_type = get_or_create_type(states.TMA_TYPE)

        if tma_uids:
            cores = []
            for tu in tma_uids:
                tma = ln.Record.get(uid=tu)
                cores += list(ln.Record.filter(type=core_type, name__startswith=f"{tma.name}:R"))
        else:
            cores = list(ln.Record.filter(type=core_type))

        # Target: attempted but produced nothing. `bgnormed` is set only after a successful write.
        targets: list[tuple] = []  # (core, failed_marker, attempts)
        for c in cores:
            cv = c.features.get_values()
            if cv.get("bgnormed") is not None:
                continue
            failed = cv.get(vocab.BGNORM_CORE_FAILED) is True
            attempts = int(cv.get(vocab.BGNORM_ATTEMPTS) or 0)
            if failed or attempts:
                targets.append((c, failed, attempts))
        if limit:
            targets = targets[:limit]

        logger.info(f"clear-bgnorm-failures: {len(targets)} core(s) to re-open (dry_run={dry_run})")

        requeueable = {states.S.BGNORM_FAILED, states.S.BGNORM_PENDING}
        if include_complete:
            requeueable.add(states.S.COMPLETE)
        already_active = {states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING}

        affected: dict[str, object] = {}  # tma uid -> TMA Record (deduped)
        cleared = 0
        for core, failed, attempts in targets:
            tma = ln.Record.filter(name=core.name.rsplit(":", 1)[0], type=tma_type).first()
            if tma is not None:
                affected[tma.uid] = tma
            print(
                f"{'PLAN' if dry_run else 'CLEAR'}: {core.name}  "
                f"(bgnorm_core_failed={failed}, bgnorm_attempts={attempts})",
                flush=True,
            )
            if dry_run:
                continue
            cv = core.features.get_values()
            for feat in vocab.BGNORM_BOOKKEEPING:
                if cv.get(feat) is not None:
                    core.features.remove_values(feat)
            cleared += 1

        # Re-queue each affected TMA so the runner relaunches the cleared cores.
        requeued: list[str] = []
        for tma in affected.values():
            state = get_state(tma)
            if state in already_active:
                logger.info(f"{tma.name}: already {state}; runner will pick the cleared core(s) up")
                continue
            if state not in requeueable:
                hint = (
                    " Pass include_complete=true to reopen it."
                    if state == states.S.COMPLETE else ""
                )
                logger.warning(f"{tma.name}: at {state!r}, not re-queued (markers cleared).{hint}")
                continue
            print(
                f"{'PLAN' if dry_run else 'REQUEUE'}: {tma.name} ({state} -> bgnorm_queued)",
                flush=True,
            )
            if not dry_run:
                set_state(tma, states.S.BGNORM_QUEUED)
            requeued.append(tma.name)

        logger.info(
            f"clear-bgnorm-failures done: targeted={len(targets)} cleared={cleared} "
            f"tmas_requeued={len(requeued)} (dry_run={dry_run})"
        )
        return {
            "targeted": len(targets),
            "cleared": cleared,
            "tmas_requeued": requeued,
            "dry_run": dry_run,
        }
    finally:
        ln.finish()