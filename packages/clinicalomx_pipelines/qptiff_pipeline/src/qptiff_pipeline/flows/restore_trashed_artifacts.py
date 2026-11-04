"""restore-trashed-artifacts: un-trash every trashed artifact an active record still references.

Metadata only, through ``artifact.restore()``: no bytes move and no keys change. Trashed artifacts
referenced only by trashed records, or by nothing, are left for cleanup.

``dry_run=True`` (default) reports the plan. Idempotent. Runs on the HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="restore-trashed-artifacts", log_prints=True, retries=1, retry_delay_seconds=60)
def restore_trashed_artifacts(
    storage_root: str,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict:
    """Un-trash any trashed artifact still referenced by an active record. Metadata only.

    limit: cap on restores this run (None = all).
    """
    logger = get_run_logger()

    import os
    from collections import Counter

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

    restored = only_trashed_ref = free = 0
    by_prefix: Counter = Counter()
    for a in ln.Artifact.filter(storage=db, branch_id=-1):
        ras = list(RecordArtifact.objects.filter(value_id=a.id))
        if not ras:
            free += 1
            continue
        active_refs = [ra for ra in ras if ra.record is not None and ra.record.branch_id == 1]
        if not active_refs:
            only_trashed_ref += 1  # only trashed records point here; leave it
            continue
        top = (a.key or "").split("/")[0] or "<empty>"
        by_prefix[top] += 1
        print(
            f"{'PLAN-RESTORE' if dry_run else 'RESTORE'}: {a.key}  "
            f"(referenced by {len(active_refs)} active record(s))",
            flush=True,
        )
        if dry_run:
            continue
        if limit is not None and restored >= limit:
            continue
        try:
            a.restore()  # bytes and key untouched
            restored += 1
        except Exception as exc:
            logger.warning(f"restore {a.key}: {exc}")

    logger.info(
        f"restore-trashed-artifacts done: restored={restored} "
        f"skipped_only_trashed_ref={only_trashed_ref} skipped_free={free} dry_run={dry_run}"
    )
    if by_prefix:
        logger.info("restored by top-level key prefix: " + ", ".join(f"{k}={c}" for k, c in by_prefix.most_common()))
    return {
        "restored": restored,
        "skipped_only_trashed_ref": only_trashed_ref,
        "skipped_free": free,
        "by_prefix": dict(by_prefix),
        "dry_run": dry_run,
    }
