"""delete-byteless-artifacts: permanently delete dbstorage Artifact records whose bytes are missing.

Deletes an artifact only if its bytes are missing and no record references it. Referenced ones are
reported and left alone. Deletes the row only (``permanent=True, storage=False``).

``dry_run=True`` (default) reports the plan. Runs on the HPC, where it can check for the bytes.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="delete-byteless-artifacts", log_prints=True, retries=1, retry_delay_seconds=60)
def delete_byteless_artifacts(
    storage_root: str,
    dry_run: bool = True,
    include_trashed: bool = True,
    limit: int | None = None,
) -> dict:
    """Permanently delete artifacts whose bytes are missing and that no record references.

    include_trashed: also consider trashed (branch_id=-1) artifacts.
    limit: cap on deletions this run (None = all).
    """
    logger = get_run_logger()

    import os
    from pathlib import Path

    import lamindb as ln
    from lamindb.models import RecordArtifact

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

    arts = list(ln.Artifact.filter(storage=db))
    if include_trashed:
        arts += list(ln.Artifact.filter(storage=db, branch_id=-1))

    deleted = skipped_present = skipped_referenced = 0
    for a in arts:
        try:
            present = Path(str(a.path)).exists()
        except Exception:
            present = False
        if present:
            skipped_present += 1
            continue
        if RecordArtifact.objects.filter(value_id=a.id).exists():
            print(f"SKIP-REFERENCED (byteless but a record still points here): {a.key}", flush=True)
            skipped_referenced += 1
            continue
        print(f"{'PLAN-DELETE' if dry_run else 'DELETE'} (byteless): {a.key}", flush=True)
        if dry_run:
            continue
        if limit is not None and deleted >= limit:
            continue
        try:
            a.delete(permanent=True, storage=False)  # row only; bytes already gone
            deleted += 1
        except Exception as exc:
            logger.warning(f"delete {a.uid} ({a.key}): {exc}")

    logger.info(
        f"delete-byteless-artifacts done: scanned={len(arts)} deleted={deleted} "
        f"skipped_present={skipped_present} skipped_referenced={skipped_referenced} dry_run={dry_run}"
    )
    return {
        "scanned": len(arts),
        "deleted": deleted,
        "skipped_present": skipped_present,
        "skipped_referenced": skipped_referenced,
        "dry_run": dry_run,
    }
