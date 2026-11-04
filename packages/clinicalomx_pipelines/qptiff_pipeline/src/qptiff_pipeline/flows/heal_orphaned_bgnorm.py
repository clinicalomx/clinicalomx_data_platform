"""heal-orphaned-bgnorm: re-open cores whose ``bgnormed`` zarr bytes are gone.

A core is orphaned when ``bgnormed`` is set but resolves to no Artifact, or to one with no bytes on
disk. For each, it clears ``bgnormed``, that run's ``bgnorm_qc`` and the runner's markers, trashes
the orphan Artifact records, and re-queues the TMA to ``bgnorm_queued``.

``dry_run=True`` (default) only logs the plan. Runs on the HPC, where it can check for the bytes.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="heal-orphaned-bgnorm", log_prints=True, retries=1, retry_delay_seconds=60)
def heal_orphaned_bgnorm(
    dry_run: bool = True,
    tma_uids: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Re-open cores whose bgnormed zarr bytes are missing, so the runner regenerates them.

    tma_uids: restrict the scan to these TMAs (default None = every Core record).
    limit: cap on cores healed this run (None = all).
    """
    logger = get_run_logger()

    from pathlib import Path

    import lamindb as ln

    # newest_artifact: the orphan check asks about the current result.
    from clinicalomx import newest_artifact as _art
    from clinicalomx import set_state

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("heal-orphaned-bgnorm")
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

        # Orphan = bgnormed is set but resolves to no artifact, or to one whose bytes are gone.
        orphans: list[tuple] = []  # (core, artifact | None)
        for c in cores:
            raw = c.features.get_values().get("bgnormed")
            if raw is None:
                continue
            art = _art(raw)
            if art is not None and Path(str(art.path)).exists():
                continue  # healthy: real artifact with bytes on disk
            orphans.append((c, art))
        if limit:
            orphans = orphans[:limit]

        logger.info(f"heal-orphaned-bgnorm: {len(orphans)} orphaned core(s) (dry_run={dry_run})")
        affected: dict[str, object] = {}  # tma uid -> tma Record (deduped)
        healed = 0
        for core, art in orphans:
            tma_name = core.name.rsplit(":", 1)[0]
            dead = getattr(art, "key", art) if art is not None else "(no artifact record)"
            print(f"{'PLAN' if dry_run else 'HEAL'}: {core.name}  (dead bgnormed: {dead})", flush=True)
            tma = ln.Record.filter(name=tma_name, type=tma_type).first()
            if tma is not None:
                affected[tma.uid] = tma
            if dry_run:
                continue
            # Clear the dead reference, that run's QC table and the runner's markers. A core with
            # bgnormed=None is needed again.
            cv = core.features.get_values()
            qc_art = _art(cv.get(vocab.BGNORM_QC_FEATURE))
            for feat in (
                vocab.BGNORMED,
                vocab.BGNORM_QC_FEATURE,
                *vocab.BGNORM_BOOKKEEPING,
            ):
                if cv.get(feat) is not None:
                    core.features.remove_values(feat)
            # Trash the records only. cleanup-storage removes any remaining bytes later.
            for dead_art in (art, qc_art):
                if dead_art is None:
                    continue
                try:
                    dead_art.delete()
                except Exception as exc:
                    logger.warning(f"trash orphan {getattr(dead_art, 'key', dead_art)}: {exc}")
            healed += 1

        # Re-queue each affected TMA so the bgnorm-runner regenerates its cores.
        for tma in affected.values():
            print(f"{'PLAN' if dry_run else 'REQUEUE'}: {tma.name} -> bgnorm_queued", flush=True)
            if not dry_run:
                set_state(tma, states.S.BGNORM_QUEUED)

        logger.info(
            f"heal-orphaned-bgnorm done: orphans={len(orphans)} healed={healed} "
            f"tmas_requeued={len(affected)}"
        )
        return {
            "orphans": len(orphans),
            "healed": healed,
            "tmas_requeued": [t.name for t in affected.values()],
            "dry_run": dry_run,
        }
    finally:
        ln.finish()
