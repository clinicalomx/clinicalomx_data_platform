"""reset-tma-cores: reprocess a TMA from the core stage.

Deletes the TMA's Core records, trashes its core and bgnorm artifacts, and sets the TMA back to
``registered`` so core-detect runs again. Keeps the qptiff, the WholeSlide and the TMA Record,
including the slide's general ROI parquet that core-detect crops from.

``dry_run=True`` (default) logs the plan. Runs on the HPC, where the artifact bytes are.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="reset-tma-cores", log_prints=True, retries=1, retry_delay_seconds=60)
def reset_tma_cores(
    tma_uids: list[str] | None = None,
    tma_names: list[str] | None = None,
    dry_run: bool = True,
) -> dict:
    """Delete each TMA's Core records and core/bgnorm artifacts, and set it back to ``registered``.

    Pass ``tma_uids`` or ``tma_names`` (e.g. ``["Synthetic_TMA01_Scan1-16-bit:TMA0"]``), or both.
    After a non-dry run, run core-detect on these TMAs.
    """
    logger = get_run_logger()

    import lamindb as ln
    from clinicalomx import all_artifacts, set_state

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("reset-tma-cores")
    try:
        tma_type = get_or_create_type(states.TMA_TYPE)
        core_type = get_or_create_type(states.CORE_TYPE)

        # Resolve the target TMA Records, deduped by uid.
        targets: dict[str, object] = {}
        for uid in tma_uids or []:
            rec = ln.Record.filter(uid=uid, type=tma_type).one_or_none()
            if rec is None:
                logger.warning(f"no TMA with uid={uid!r}; skipping")
            else:
                targets[rec.uid] = rec
        for name in tma_names or []:
            rec = ln.Record.filter(name=name, type=tma_type).one_or_none()
            if rec is None:
                logger.warning(f"no TMA named {name!r}; skipping")
            else:
                targets[rec.uid] = rec
        if not targets:
            raise ValueError("pass at least one existing tma_uids or tma_names entry")

        results: list[dict] = []
        for tma in targets.values():
            cores = list(ln.Record.filter(type=core_type, name__startswith=f"{tma.name}:R"))

            # Artifacts to trash: each Core's bgnorm zarr and the cores parquet, and the TMA's
            # core-stage
            # detection artifacts. Never the TMA's source_artifact: core-detect crops from it.
            arts: dict[str, object] = {}

            # `arts` is bound as a default, so each unit's _mark writes to that unit's dict.
            def _mark(a, arts=arts) -> None:
                # `all_artifacts` handles an Artifact, a key string or a list. Trash every version.
                for art in all_artifacts(a):
                    arts[art.uid] = art

            for c in cores:
                cv = c.features.get_values()
                _mark(cv.get("bgnormed"))
                _mark(cv.get(vocab.BGNORM_QC_FEATURE))
                _mark(cv.get("source_artifact"))  # the TMA's cores parquet
            tv = tma.features.get_values()
            for f in (vocab.F_THUMBNAIL, vocab.F_PRED, vocab.F_DET_OVERLAY, vocab.F_DEARRAY_OVERLAY):
                _mark(tv.get(f))

            print(
                f"{'PLAN' if dry_run else 'RESET'}: {tma.name}: {len(cores)} core(s), "
                f"{len(arts)} artifact(s) -> registered",
                flush=True,
            )
            if dry_run:
                results.append({"tma": tma.name, "cores": len(cores), "artifacts": len(arts)})
                continue

            for c in cores:
                try:
                    c.delete()
                except Exception as exc:
                    logger.warning(f"core delete {c.name}: {exc}")
            for a in arts.values():
                try:
                    a.delete()  # trash (recoverable), also when the bytes are gone
                except Exception as exc:
                    logger.warning(f"artifact trash {getattr(a, 'key', a)}: {exc}")
            set_state(tma, states.S.REGISTERED)
            logger.info(
                f"{tma.name}: reset -> registered ({len(cores)} core(s) deleted, "
                f"{len(arts)} artifact(s) trashed)"
            )
            results.append({"tma": tma.name, "cores": len(cores), "artifacts": len(arts)})

        logger.info(f"reset-tma-cores done: processed={len(results)} dry_run={dry_run}")
        return {"processed": len(results), "results": results, "dry_run": dry_run}
    finally:
        ln.finish()
