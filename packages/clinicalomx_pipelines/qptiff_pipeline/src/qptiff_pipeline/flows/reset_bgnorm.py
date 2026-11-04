"""reset-bgnorm: delete bgnorm outputs so the targeted cores re-run from scratch.

For each targeted Core it permanently deletes every bgnorm artifact in the core's folder (zarrs and
QC parquets, all runs): bytes, catalog row and feature value. It clears the runner's markers and
the TMA's result features, then re-queues the TMA to ``bgnorm_queued``. Other derivatives of the
core are untouched. MLflow is not touched.

Target by ``tma_uids``, ``tma_names`` or ``slide_uids``, or set ``all_tmas=True`` with
``confirm="wipe-every-bgnorm"``. A ``complete`` TMA is skipped unless ``include_complete=True``. A
TMA upstream of bgnorm has its cores cleared but its state left alone. A TMA at ``bgnorm_running``
is reset with a warning: stop the runner first, or live jobs may write the zarrs this deletes.

``dry_run=True`` (default) only logs the plan, with artifact count and byte total. Runs on the HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

#: The token a non-dry ``all_tmas`` run requires.
CONFIRM_ALL = "wipe-every-bgnorm"


def _bytes_of(arts) -> tuple[int, int]:
    """``(total bytes, artifacts whose size is unrecorded)`` over a list of Artifacts.

    Uses each Artifact's recorded ``size``. Unrecorded sizes are counted, so the plan says when the
    total is an underestimate.
    """
    total = unknown = 0
    for a in arts:
        n = getattr(a, "size", None)
        if n is None:
            unknown += 1
        else:
            total += int(n)
    return total, unknown


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


@flow(name="reset-bgnorm", log_prints=True, retries=1, retry_delay_seconds=60)
def reset_bgnorm(
    tma_uids: list[str] | None = None,
    tma_names: list[str] | None = None,
    slide_uids: list[str] | None = None,
    all_tmas: bool = False,
    confirm: str = "",
    include_complete: bool = False,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict:
    """Delete each targeted Core's bgnorm outputs, clear its markers, re-queue the TMA for bgnorm.

    tma_uids / tma_names / slide_uids: which units to reset. slide_uids expands to every TMA of
        that WholeSlide.
    all_tmas: reset every TMA on the instance. Excludes the three above and needs ``confirm``.
    confirm: must equal ``"wipe-every-bgnorm"`` for a non-dry ``all_tmas`` run.
    include_complete: also reset TMAs a reviewer accepted (``complete``). Default False.
    dry_run: True (default) only logs the plan. False deletes bytes permanently.
    limit: approximate cap on cores reset this run (None = all). A TMA is never split: the run stops
        before a TMA that would exceed it. Re-run to continue.
    """
    logger = get_run_logger()

    from pathlib import Path

    import lamindb as ln

    # all_artifacts: delete every version of a role feature, not just the newest.
    from clinicalomx import all_artifacts, get_state, set_state

    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    def _all_bgnorm_artifacts(core, cv) -> list:
        """Every bgnorm artifact of a core: all runs and legacy names, plus the feature-pointed
        ones.

        Sweeps the core's folder for basenames starting ``bgnorm``.
        """
        found: dict[str, object] = {}
        for feat in (vocab.BGNORMED, vocab.BGNORM_QC_FEATURE):
            # all_artifacts, not the newest: a re-run leaves several under one name.
            for a in all_artifacts(cv.get(feat)):
                found[a.uid] = a
        src = cv.get("source_image")
        qkey = src.key if isinstance(src, ln.Artifact) else src
        if qkey:
            prefix = f"{keys.core_dir(str(qkey), core.name)}/bgnorm"
            for a in ln.Artifact.filter(key__startswith=prefix):
                found[a.uid] = a
        return list(found.values())

    def _sever_links(art) -> None:
        # Hard-delete this artifact's link rows in both tables: either one blocks deletion.
        # RecordArtifact stores it as `value`, ArtifactRecord as `artifact`.
        from lamindb.models import ArtifactRecord, RecordArtifact

        for model, field in ((RecordArtifact, "value"), (ArtifactRecord, "artifact")):
            try:
                qs = model.objects.filter(**{field: art})
                qs._raw_delete(qs.db)
            except Exception as exc:
                logger.warning(f"sever {model.__name__} for {getattr(art, 'key', art)}: {exc}")

    track_prefect_run("reset-bgnorm")
    try:
        core_type = get_or_create_type(states.CORE_TYPE)
        tma_type = get_or_create_type(states.TMA_TYPE)

        named = bool(tma_uids or tma_names or slide_uids)
        if not (named or all_tmas):
            raise ValueError(
                "reset-bgnorm needs at least one of tma_uids / tma_names / slide_uids, "
                "or all_tmas=True (refusing to reset every TMA by omission)"
            )
        if named and all_tmas:
            raise ValueError(
                "pass EITHER all_tmas=True OR named targets, not both - a named subset alongside "
                "'everything' reads as a scoped run and is not one"
            )
        if all_tmas and not dry_run and confirm != CONFIRM_ALL:
            raise ValueError(
                f"refusing the population-wide wipe: pass confirm='{CONFIRM_ALL}' to apply"
            )

        # Resolve the target TMAs (deduped), then their Core records.
        tmas: dict[str, object] = {}  # uid -> TMA Record
        if all_tmas:
            for t in ln.Record.filter(type=tma_type):
                tmas[t.uid] = t
        for tu in tma_uids or []:
            t = ln.Record.get(uid=tu)
            tmas[t.uid] = t
        for tn in tma_names or []:
            t = ln.Record.filter(name=tn, type=tma_type).first()
            if t is not None:
                tmas[t.uid] = t
            else:
                logger.warning(f"tma_name {tn!r} not found; skipping")
        for su in slide_uids or []:
            slide = ln.Record.get(uid=su)
            src = slide.features.get_values().get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else (ln.Artifact.get(key=src) if src else None)
            if qptiff is None:
                logger.warning(f"slide {slide.name!r} has no source_image; skipping")
                continue
            base = Path(qptiff.key).stem
            for t in ln.Record.filter(type=tma_type, name__startswith=f"{base}:TMA"):
                tmas[t.uid] = t

        # Re-queue table, shared with clear-bgnorm-failures. A TMA upstream of bgnorm is cleared but
        # not re-queued, so it never skips its core gate.
        requeueable = {states.S.BGNORM_FAILED, states.S.BGNORM_PENDING}
        if include_complete:
            requeueable.add(states.S.COMPLETE)
        already_active = {states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING}

        ordered = sorted(tmas.values(), key=lambda t: t.name)
        logger.info(
            f"reset-bgnorm: {len(ordered)} TMA(s) targeted (dry_run={dry_run}, "
            f"all_tmas={all_tmas}, include_complete={include_complete}, limit={limit})"
        )

        reset = deleted = 0
        planned_bytes = freed_bytes = unknown_size = 0
        requeued: list[str] = []
        skipped_accepted: list[str] = []
        warned_running: list[str] = []
        not_requeued: list[str] = []
        touched: list[str] = []
        budget_stopped = False

        # TMA by TMA: each is planned, applied and re-queued before the next starts.
        for tma in ordered:
            state = get_state(tma)
            if state == states.S.COMPLETE and not include_complete:
                logger.warning(
                    f"{tma.name}: at 'complete' - a reviewer ACCEPTED this result, so it is left "
                    "untouched (bytes, records and state). Pass include_complete=true to reset it."
                )
                skipped_accepted.append(tma.name)
                continue

            cores = list(ln.Record.filter(type=core_type, name__startswith=f"{tma.name}:R"))
            if limit is not None and touched and reset + len(cores) > limit:
                # Stop taking new TMAs rather than split this one.
                budget_stopped = True
                break

            if state == states.S.BGNORM_RUNNING:
                logger.warning(
                    f"{tma.name}: at 'bgnorm_running' - its cores may have live sbatch jobs "
                    "writing the zarrs this deletes. Quiesce the runner before applying."
                )
                warned_running.append(tma.name)

            per_tma_arts: list = []
            per_core: list[tuple] = []
            for core in cores:
                cv = core.features.get_values()
                # Every bgnorm artifact of this core, all runs and legacy names.
                arts = _all_bgnorm_artifacts(core, cv)
                per_core.append((core, cv, arts))
                per_tma_arts += arts
            nbytes, unknown = _bytes_of(per_tma_arts)
            planned_bytes += nbytes
            unknown_size += unknown

            print(
                f"{'PLAN' if dry_run else 'RESET'}: {tma.name}  ({state})  "
                f"{len(cores)} core(s), {len(per_tma_arts)} artifact(s), {_gb(nbytes)}"
                + (f", {unknown} of unrecorded size" if unknown else ""),
                flush=True,
            )
            for core, _cv, arts in per_core:
                # A core with no artifacts still has its markers cleared: bgnorm_core_failed=True
                # makes the runner skip it.
                tags = (
                    ", ".join(a.key.rsplit("/", 1)[-1] for a in arts)
                    if arts else "nothing (clear markers only)"
                )
                print(f"  {'PLAN' if dry_run else 'RESET'}: {core.name}  (trash: {tags})", flush=True)

            touched.append(tma.name)
            if not dry_run:
                for core, cv, arts in per_core:
                    # 1) drop the feature values and the runner's markers, so the core is needed
                    # again.
                    for feat in (
                        vocab.BGNORMED,
                        vocab.BGNORM_QC_FEATURE,
                        *vocab.BGNORM_BOOKKEEPING,
                    ):
                        if cv.get(feat) is not None:
                            core.features.remove_values(feat)
                    # 2) delete each artifact's link rows, then its row and bytes.
                    for art in arts:
                        tag = art.key
                        size = getattr(art, "size", None) or 0
                        _sever_links(art)
                        try:
                            art.delete(permanent=True, storage=True)
                            deleted += 1
                            freed_bytes += size
                        except Exception:
                            try:
                                art.delete(permanent=True, storage=False)  # bytes already gone
                                deleted += 1
                            except Exception as exc:
                                logger.warning(f"delete artifact {tag}: {exc}")
                    reset += 1
                # 3) the TMA's own result features, which describe the deleted run.
                tv = tma.features.get_values()
                for feat in vocab.BGNORM_TMA_RESULT:
                    if tv.get(feat) is not None:
                        tma.features.remove_values(feat)
            else:
                reset += len(cores)

            # 4) re-queue this TMA before starting the next.
            if state in already_active:
                logger.info(f"{tma.name}: already {state}; runner will pick the reset core(s) up")
                continue
            if state not in requeueable:
                hint = (
                    " Pass include_complete=true to reopen it."
                    if state == states.S.COMPLETE else
                    " It is parked upstream of bgnorm; advance it through its own gate."
                )
                logger.warning(f"{tma.name}: at {state!r}, not re-queued (cores cleared).{hint}")
                not_requeued.append(tma.name)
                continue
            print(
                f"{'PLAN' if dry_run else 'REQUEUE'}: {tma.name} ({state} -> bgnorm_queued)",
                flush=True,
            )
            if not dry_run:
                set_state(tma, states.S.BGNORM_QUEUED)
            requeued.append(tma.name)

        if budget_stopped:
            logger.info(
                f"reset-bgnorm: stopped at the limit={limit} core budget after {len(touched)} "
                f"whole TMA(s); re-run to resume at the next one"
            )
        logger.info(
            f"reset-bgnorm done: tmas_reset={len(touched)} cores_reset={reset} "
            f"artifacts_deleted={deleted} bytes_planned={_gb(planned_bytes)} "
            f"bytes_freed={_gb(freed_bytes)} tmas_requeued={len(requeued)} "
            f"skipped_accepted={len(skipped_accepted)} (dry_run={dry_run})"
        )
        return {
            "tmas_targeted": [t.name for t in ordered],
            "tmas_reset": touched,
            "cores_reset": reset,
            "artifacts_deleted": deleted,
            "bytes_planned": planned_bytes,
            "bytes_freed": freed_bytes,
            "artifacts_of_unrecorded_size": unknown_size,
            "tmas_requeued": requeued,
            "skipped_accepted": skipped_accepted,
            "warned_running": warned_running,
            "not_requeued": not_requeued,
            "budget_stopped": budget_stopped,
            "dry_run": dry_run,
        }
    finally:
        ln.finish()
