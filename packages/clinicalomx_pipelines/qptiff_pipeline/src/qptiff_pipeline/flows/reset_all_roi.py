"""reset-all-roi: DESTRUCTIVE. Delete all ROI records and all derived artifacts.

Deletes every Core, TMA and WholeSlide Record, every Artifact in the dbstorage Storage, and every
Collection whose members are all such artifacts. Keeps the raw ``.qptiff`` artifacts, which live in
another Storage, and every Collection with a member elsewhere. Re-run mint-slides and the stages
after it to rebuild. :func:`_scope` is the only place that decides what may be deleted.

``dry_run=True`` (default) reports counts only. ``confirm`` must be ``"wipe-clinicalomx"`` to apply.
Runs on the HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


def _owned_collections(members: dict[int, set[int]], artifact_ids: set[int]) -> set[int]:
    """The collections this flow may delete: those whose every member is an artifact it deletes.

    ``members`` maps a collection id to all its member artifact ids, trashed members included.
    """
    return {c for c, arts in members.items() if arts and arts <= artifact_ids}


def _scope(ln, db) -> dict:
    """Everything this flow may delete, derived from the Storage ``db`` and the ROI record types.

    Collections are reached only through artifacts in ``db``.
    """
    from lamindb.models import CollectionArtifact

    from qptiff_pipeline.domain import states
    from qptiff_pipeline.lamin.registry import get_or_create_type

    cores = list(ln.Record.filter(type=get_or_create_type(states.CORE_TYPE)))
    tmas = list(ln.Record.filter(type=get_or_create_type(states.TMA_TYPE)))
    slides = list(ln.Record.filter(type=get_or_create_type(states.SLIDE_TYPE)))
    arts = list(ln.Artifact.filter(storage=db)) + list(ln.Artifact.filter(storage=db, branch_id=-1))
    art_ids = {a.id for a in arts}

    touching = CollectionArtifact.objects.filter(artifact_id__in=art_ids).values("collection_id")
    members: dict[int, set[int]] = {}
    for cid, aid in CollectionArtifact.objects.filter(collection_id__in=touching).values_list(
        "collection_id", "artifact_id"
    ):
        members.setdefault(cid, set()).add(aid)
    owned = _owned_collections(members, art_ids)
    colls = list(ln.Collection.objects.filter(id__in=owned))

    return {
        "records": (("Core", cores), ("TMA", tmas), ("WholeSlide", slides)),
        "record_ids": {r.id for r in cores + tmas + slides},
        "artifacts": arts,
        "artifact_ids": art_ids,
        "collections": colls,
        # Linked to a dbstorage artifact but kept, because another member lives elsewhere. Its link
        # blocks that artifact's delete, which is logged.
        "collections_shared": sorted(set(members) - owned),
    }


@flow(name="reset-all-roi", log_prints=True, retries=0)
def reset_all_roi(
    storage_root: str,
    dry_run: bool = True,
    confirm: str = "",
) -> dict:
    """Delete all ROI records, all dbstorage artifacts, and the collections made only of them.

    confirm: must equal "wipe-clinicalomx" to delete (ignored in dry_run).
    """
    logger = get_run_logger()

    import os

    import lamindb as ln

    from qptiff_pipeline.lamin.tracking import track_prefect_run

    REQUIRED = "wipe-clinicalomx"
    if not dry_run and confirm != REQUIRED:
        raise ValueError(f"refusing to wipe: pass confirm='{REQUIRED}' to apply (dry_run=False)")

    real = os.path.realpath(storage_root)
    db = ln.Storage.filter(root=storage_root).one_or_none()
    if db is None:
        for s in ln.Storage.filter():
            try:
                if os.path.realpath(str(s.root)) == real:
                    db = s
                    break
            except Exception:
                continue
    if db is None:
        raise ValueError(f"no lamindb Storage resolves to {real!r}; refusing to run")

    track_prefect_run("reset-all-roi")
    try:
        scope = _scope(ln, db)
        colls, arts = scope["collections"], scope["artifacts"]
        n_records = {label: len(recs) for label, recs in scope["records"]}
        shared = scope["collections_shared"]

        logger.info(
            f"reset-all-roi PLAN (dry_run={dry_run}): cores={n_records['Core']} "
            f"tmas={n_records['TMA']} slides={n_records['WholeSlide']} collections={len(colls)} "
            f"dbstorage_artifacts={len(arts)} | raw qptiffs (other storages) and every collection "
            f"with a member outside dbstorage are KEPT"
            + (f"; {len(shared)} KEPT collection(s) share a member with dbstorage, and those "
               f"artifacts will not delete: ids {shared}" if shared else "")
        )
        if dry_run:
            return {
                "dry_run": True, "cores": n_records["Core"], "tmas": n_records["TMA"],
                "slides": n_records["WholeSlide"], "collections": len(colls),
                "collections_shared": len(shared), "dbstorage_artifacts": len(arts),
            }

        # 0) Delete the record<->artifact link rows first: records and artifacts block each other's
        # deletion. Only rows touching an ROI record or a dbstorage artifact. lamindb's .delete()
        # does nothing on link tables, so these use a raw SQL delete. Covers ArtifactRecord,
        # RecordArtifact and RecordRecord.
        from lamindb.models import ArtifactRecord, RecordArtifact, RecordRecord

        rec_ids, art_ids = scope["record_ids"], scope["artifact_ids"]
        link_rows = (
            ("RecordArtifact", RecordArtifact, {"record_id__in": rec_ids}, {"value_id__in": art_ids}),
            ("ArtifactRecord", ArtifactRecord, {"record_id__in": rec_ids}, {"artifact_id__in": art_ids}),
            ("RecordRecord", RecordRecord, {"record_id__in": rec_ids}, {"value_id__in": rec_ids}),
        )
        for label, model, one_end, other_end in link_rows:
            try:
                qs = model.objects.filter(**one_end) | model.objects.filter(**other_end)
                n = qs._raw_delete(qs.db)  # hard SQL DELETE, no cascade, protection or soft delete
                logger.info(f"raw-severed {label}: {n} rows")
            except Exception as exc:
                logger.warning(f"raw-sever {label} failed: {exc}")

        # 1) records leaf-first, then collections, which drops the links that block artifacts.
        rec_deleted = 0
        for label, recs in scope["records"]:
            for r in recs:
                try:
                    try:
                        r.delete(permanent=True)
                    except TypeError:
                        r.delete()
                    rec_deleted += 1
                except Exception as exc:
                    logger.warning(f"delete {label} {r.name}: {exc}")
            logger.info(f"deleted {label} records: {rec_deleted}")
        # Permanent: every one is owned by this run (see _scope). A trashed collection would keep
        # the links that block step 2.
        coll_deleted = 0
        for c in colls:
            try:
                c.delete(permanent=True)
                coll_deleted += 1
            except Exception as exc:
                logger.warning(f"delete collection {getattr(c, 'key', c)}: {exc}")

        # 2) the now-unreferenced dbstorage artifacts: row and bytes.
        art_deleted = 0
        for a in arts:
            try:
                a.delete(permanent=True, storage=True)
                art_deleted += 1
            except Exception:
                try:
                    a.delete(permanent=True, storage=False)  # bytes gone or still referenced
                    art_deleted += 1
                except Exception as exc2:
                    logger.warning(f"delete artifact {getattr(a, 'key', a)}: {exc2}")

        logger.info(
            f"reset-all-roi done: records_deleted={rec_deleted} collections_deleted={coll_deleted} "
            f"artifacts_deleted={art_deleted}. Re-run mint-slides -> general-detect to repopulate."
        )
        return {
            "dry_run": False, "records_deleted": rec_deleted,
            "collections_deleted": coll_deleted, "artifacts_deleted": art_deleted,
        }
    finally:
        ln.finish()
