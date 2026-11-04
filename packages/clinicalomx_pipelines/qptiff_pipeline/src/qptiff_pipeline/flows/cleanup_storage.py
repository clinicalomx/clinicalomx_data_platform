"""cleanup-storage: DESTRUCTIVE. Reclaim disk under the dbstorage root.

1. Purges trashed artifacts (``branch_id=-1``) that no record references: row and bytes.
2. Deletes ``.lamindb/<uid>`` blobs that have no Artifact row.

Both are irreversible. ``dry_run=True`` (default) only reports the manifest. Scoped to one Storage,
resolved by real path. Runs on the HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="cleanup-storage", log_prints=True, retries=1, retry_delay_seconds=60)
def cleanup_storage(
    storage_root: str,
    dry_run: bool = True,
    purge_trashed: bool = True,
    delete_orphans: bool = True,
    limit: int | None = None,
) -> dict:
    """Purge trashed artifacts and delete orphaned .lamindb blobs in dbstorage.

    purge_trashed: permanently delete unreferenced ``branch_id=-1`` artifacts (row and bytes).
    delete_orphans: remove ``.lamindb/<uid>`` files and dirs that have no artifact row.
    limit: cap on items handled per phase this run (None = all).
    """
    logger = get_run_logger()

    import os
    import shutil
    from pathlib import Path

    import lamindb as ln

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
        raise ValueError(f"no lamindb Storage resolves to {real!r}; refusing to clean up")
    root = Path(str(db.root))

    result: dict = {"dry_run": dry_run, "storage_root": str(root)}

    # 1) Permanently purge trashed artifacts no record references. Referenced ones are skipped and
    # reported.
    from lamindb.models import RecordArtifact

    purged = referenced_skipped = 0
    if purge_trashed:
        trashed = list(ln.Artifact.filter(storage=db, branch_id=-1))
        if limit:
            trashed = trashed[:limit]
        logger.info(f"cleanup: {len(trashed)} trashed artifact(s) considered (dry_run={dry_run})")
        for a in trashed:
            if RecordArtifact.objects.filter(value_id=a.id).exists():
                print(f"SKIP-REFERENCED (a record still points here): {a.key}", flush=True)
                referenced_skipped += 1
                continue
            print(f"{'PLAN-PURGE' if dry_run else 'PURGE'}: {a.uid}  {a.key}", flush=True)
            if dry_run:
                continue
            # Pass storage=True explicitly: lamindb otherwise prompts and hangs a SLURM run. If the
            # bytes are already gone, retry with storage=False so the row still clears.
            try:
                a.delete(permanent=True, storage=True)  # row + bytes
                purged += 1
            except Exception as exc:
                logger.info(f"purge {a.key}: storage delete failed ({exc}); removing row only")
                try:
                    a.delete(permanent=True, storage=False)  # row only; bytes already gone
                    purged += 1
                except Exception as exc2:
                    logger.warning(f"purge {a.uid} ({a.key}) failed: {exc2}")
        result["trashed_found"] = len(trashed)
        result["referenced_skipped"] = referenced_skipped
        result["purged"] = purged

    # 2) Delete orphaned .lamindb blobs: bytes with no artifact row.
    orphan_blobs = 0
    if delete_orphans:
        lamindb_dir = root / ".lamindb"
        entries = [e for e in os.scandir(lamindb_dir) if e.name != "storage_uid.txt"] if lamindb_dir.exists() else []
        candidates = []
        for e in entries:
            # The blob name is "<uid>[<version>]<suffix>". Any artifact with that uid prefix,
            # active or
            # trashed, means it is not an orphan.
            stem = e.name.split(".", 1)[0]
            uid16 = stem[:16]
            if ln.Artifact.filter(uid__startswith=uid16).exists():
                continue
            candidates.append(e)
        if limit:
            candidates = candidates[:limit]
        logger.info(f"cleanup: {len(candidates)} orphaned .lamindb blob(s) to delete (dry_run={dry_run})")
        for e in candidates:
            print(f"{'PLAN-ORPHAN' if dry_run else 'DELETE-ORPHAN'}: .lamindb/{e.name}", flush=True)
            if dry_run:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path)
                else:
                    os.remove(e.path)
                orphan_blobs += 1
            except Exception as exc:
                logger.warning(f"delete orphan {e.name}: {exc}")
        result["orphan_blobs_found"] = len(candidates)
        result["orphan_blobs_deleted"] = orphan_blobs

    logger.info(f"cleanup-storage done: {result}")
    return result
