"""sweep-orphan-bgnorm-zarrs: DESTRUCTIVE. Delete bgnorm output on disk that no Artifact references.

A bgnorm-core run that dies part-way leaves unregistered bytes: a partial zarr, or a QC parquet
without a row. This sweeps both under each Core's folder. An entry is orphaned when no Artifact,
active or trashed, carries its key. Results of earlier successful runs are registered and kept.

Anything modified within ``min_age_minutes`` (default 60) is skipped, because a live write looks
the same as an orphan. Never set it below one core's bgnorm runtime. The scan starts from Core
records, so folders of deleted Cores are not visited.

``dry_run=True`` (default) only reports what it would delete and the bytes reclaimed. Runs on the
HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


def _entry_bytes(path) -> int:
    """Size of a zarr directory tree or a parquet file. Unreadable parts count as 0."""
    import os

    if not os.path.isdir(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


@flow(name="sweep-orphan-bgnorm-zarrs", log_prints=True, retries=1, retry_delay_seconds=60)
def sweep_orphan_bgnorm_zarrs(
    storage_root: str,
    dry_run: bool = True,
    tma_uids: list[str] | None = None,
    min_age_minutes: int = 60,
    limit: int | None = None,
) -> dict:
    """Delete unregistered bgnorm zarrs and QC parquets under each Core's folder.

    tma_uids: restrict the scan to these TMAs (default None = every Core record).
    storage_root: dbstorage root, matched to the registered Storage by real path.
    min_age_minutes: skip entries modified more recently than this. Must exceed one core's bgnorm
        runtime.
    limit: cap on orphans deleted this run (None = all).
    """
    logger = get_run_logger()

    import os
    import shutil
    import time
    from pathlib import Path

    import lamindb as ln

    from qptiff_pipeline.domain import keys, states
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("sweep-orphan-bgnorm-zarrs")
    try:
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
            raise ValueError(f"no lamindb Storage resolves to {real!r}; refusing to sweep")
        root = Path(str(db.root))

        core_type = get_or_create_type(states.CORE_TYPE)
        if tma_uids:
            cores = []
            for tu in tma_uids:
                tma = ln.Record.get(uid=tu)
                cores += list(ln.Record.filter(type=core_type, name__startswith=f"{tma.name}:R"))
        else:
            cores = list(ln.Record.filter(type=core_type))
        logger.info(f"sweep-orphan-bgnorm-zarrs: scanning {len(cores)} core(s) under {root}")

        cutoff = time.time() - min_age_minutes * 60
        orphans: list[tuple] = []  # (key, path, bytes)
        too_new = 0
        for core in cores:
            cv = core.features.get_values()
            src = cv.get("source_image")
            if src is None:
                logger.warning(f"{core.name}: no source_image; cannot derive its folder, skipping")
                continue
            qkey = src.key if isinstance(src, ln.Artifact) else str(src)
            cdir = root / keys.core_dir(qkey, core.name)
            if not cdir.is_dir():
                continue
            # Both halves of a run's output: the zarr and the QC parquet beside it.
            entries = sorted(cdir.glob("bgnormed-image-*.zarr")) + sorted(
                cdir.glob("bgnorm-qc-*.parquet")
            )
            for entry in entries:
                key = f"{keys.core_dir(qkey, core.name)}/{entry.name}"
                # Any row, active or trashed, means these bytes are not orphaned.
                if ln.Artifact.filter(key=key).first() is not None:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > cutoff:
                    # Unregistered but recent: possibly a live bgnorm-core write.
                    logger.info(f"{key}: modified <{min_age_minutes}min ago; skip (may be live)")
                    too_new += 1
                    continue
                orphans.append((key, entry, _entry_bytes(entry)))
        if limit:
            orphans = orphans[:limit]

        total = sum(b for _k, _p, b in orphans)
        logger.info(
            f"sweep-orphan-bgnorm-zarrs: {len(orphans)} orphan(s), {total / 1e9:.2f} GB "
            f"reclaimable; {too_new} skipped as too recent (dry_run={dry_run})"
        )

        deleted = freed = 0
        for key, path, nbytes in orphans:
            print(
                f"{'PLAN-ORPHAN' if dry_run else 'DELETE-ORPHAN'}: {key}  ({nbytes / 1e9:.2f} GB)",
                flush=True,
            )
            if dry_run:
                continue
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                deleted += 1
                freed += nbytes
            except Exception as exc:
                logger.warning(f"delete orphan {key}: {exc}")

        logger.info(
            f"sweep-orphan-bgnorm-zarrs done: found={len(orphans)} deleted={deleted} "
            f"freed={freed / 1e9:.2f} GB skipped_recent={too_new} (dry_run={dry_run})"
        )
        return {
            "storage_root": str(root),
            "orphans_found": len(orphans),
            "bytes_reclaimable": total,
            "orphans_deleted": deleted,
            "bytes_freed": freed,
            "skipped_recent": too_new,
            "dry_run": dry_run,
        }
    finally:
        ln.finish()