"""reset-slide: tear down a WholeSlide's downstream core and bgnorm work after a cascade reopen.

For each TMA of the slide it deletes the Core Records, trashes (recoverable) the derived artifacts,
and supersedes the TMA Record. Then it moves the slide from general_resetting to general_pending.
Runs on the HPC, where the artifact bytes are.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

STAGE = "reset"


def _delete_prefect_artifacts(keys: list[str], logger) -> int:
    """Delete a unit's review and edit Prefect artifacts. Best effort."""
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import ArtifactFilter, ArtifactFilterKey

    n = 0
    try:
        with get_client(sync_client=True) as client:
            for key in keys:
                arts = client.read_artifacts(
                    artifact_filter=ArtifactFilter(key=ArtifactFilterKey(any_=[key]))
                )
                for a in arts:
                    try:
                        client.delete_artifact(a.id)
                        n += 1
                    except Exception as exc:  # a missing/locked artifact must not fail the reset
                        logger.warning(f"prefect artifact delete {key}: {exc}")
    except Exception as exc:
        logger.warning(f"prefect artifact cleanup skipped: {exc}")
    return n


@flow(name="reset-slide", log_prints=True, retries=2, retry_delay_seconds=60)
def reset_slide(
    slide_uids: list[str],
    dry_run: bool = True,
) -> dict:
    """Tear down each slide's downstream core and bgnorm work, then park it at general_pending.

    slide_uids: WholeSlide Record uids to reset (emitted by a cascade reopen).
    dry_run: report what would be removed and change nothing. Default True; the deployment sets
        False.
    """
    logger = get_run_logger()

    import lamindb as ln
    from clinicalomx import all_artifacts, set_state

    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("reset-slide")
    try:
        tma_type = get_or_create_type(states.TMA_TYPE)
        core_type = get_or_create_type(states.CORE_TYPE)

        results: list[dict] = []
        for uid in slide_uids:
            slide = ln.Record.get(uid=uid)
            src = slide.features.get_values().get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else (ln.Artifact.get(key=src) if src else None)
            if qptiff is None:
                logger.warning(f"{slide.name}: no source_image; cannot resolve TMAs, skipping")
                continue
            base = _P(qptiff.key).stem
            tmas = list(ln.Record.filter(type=tma_type, name__startswith=f"{base}:TMA"))

            arts: dict[str, object] = {}  # uid -> Artifact, deduped across TMAs/cores

            # `arts` is bound as a default, so each unit's _mark writes to that unit's dict.
            def _mark(a, arts=arts) -> None:
                # `all_artifacts` handles an Artifact, a key string or a list. Trash every version.
                for art in all_artifacts(a):
                    arts[art.uid] = art

            summary = {
                "slide": slide.name, "tmas": [], "cores_deleted": 0,
                "artifacts_trashed": 0, "prefect_artifacts_deleted": 0,
            }
            for tma in tmas:
                tv = tma.features.get_values()
                cores = list(ln.Record.filter(type=core_type, name__startswith=f"{tma.name}:R"))
                # Leaf artifacts from the Cores: cores parquet, bgnorm zarrs, QC tables.
                for c in cores:
                    cv = c.features.get_values()
                    _mark(cv.get("bgnormed"))
                    _mark(cv.get(vocab.BGNORM_QC_FEATURE))
                    _mark(cv.get("source_artifact"))
                # TMA-level derived artifacts: candidate grid, thumbnail, overlays.
                for f in (vocab.F_THUMBNAIL, vocab.F_PRED, vocab.F_DET_OVERLAY, vocab.F_DEARRAY_OVERLAY):
                    _mark(tv.get(f))

                summary["tmas"].append(tma.name)
                summary["cores_deleted"] += len(cores)

                if dry_run:
                    logger.info(
                        f"[dry-run] {tma.name}: would delete {len(cores)} core(s), supersede TMA"
                    )
                    continue

                for c in cores:
                    try:
                        c.delete()
                    except Exception as exc:
                        logger.warning(f"core delete {c.name}: {exc}")
                summary["prefect_artifacts_deleted"] += _delete_prefect_artifacts(
                    [f"core-edit-{keys.slug(tma.name)}", f"core-review-{keys.slug(tma.name)}"], logger
                )
                set_state(tma, states.S.SUPERSEDED)

            if dry_run:
                logger.info(f"[dry-run] {slide.name}: would trash {len(arts)} artifact(s) -> general_pending")
                results.append({**summary, "artifacts_to_trash": len(arts)})
                continue

            for a in arts.values():
                try:
                    a.delete()  # trash (recoverable), not permanent
                    summary["artifacts_trashed"] += 1
                except Exception as exc:
                    logger.warning(f"artifact trash {getattr(a, 'key', a)}: {exc}")

            set_state(slide, states.GATES["general"].pending)
            logger.info(f"{slide.name}: reset -> general_pending {summary}")
            results.append(summary)

        return {"processed": len(results), "results": results, "dry_run": dry_run}
    finally:
        ln.finish()
