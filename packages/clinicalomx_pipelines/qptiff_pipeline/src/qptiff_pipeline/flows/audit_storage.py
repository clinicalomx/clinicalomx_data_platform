"""audit-storage: READ-ONLY. Compare files on disk under the Storage root with the lamindb registry.

Each file, and each ``.zarr`` directory, is one leaf, classified as:

  - REGISTERED                  - an Artifact exists whose key is the leaf's path under the root
  - REGISTERED (no record link) - registered, but no record references it
  - UNREGISTERED                - bytes on disk with no Artifact

Also reports Artifacts in this Storage whose bytes are missing. Changes nothing. Runs on the HPC.
Skips non-derivative top-level entries. ``detail_prefix`` lists the leaves under one folder.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

# Top-level entries under the Storage root that are NOT managed derivatives.
_SKIP_TOP = {".lamindb", ".dbcache", "apptainer_cache", "environments", "clinical_metadata"}


def _leaves(base: _P, root: _P) -> list[str]:
    """Root-relative keys of every derivative leaf under ``base``. A ``.zarr`` dir is one leaf."""
    import os

    out: list[str] = []
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            p = _P(e.path)
            if e.is_dir(follow_symlinks=False):
                if p.name.endswith(".zarr"):
                    out.append(str(p.relative_to(root)))  # zarr dir = one leaf
                else:
                    stack.append(p)
            elif e.is_file(follow_symlinks=False):
                out.append(str(p.relative_to(root)))
    return out


@flow(name="audit-storage", log_prints=True)
def audit_storage(
    storage_root: str,
    detail_prefix: str | None = None,
    list_limit: int = 60,
    scan_parent: bool = True,
) -> dict:
    """Classify on-disk derivative leaves against the lamindb registry. Read-only.

    storage_root: the Storage root to walk (the dbstorage mount).
    detail_prefix: if set, list every leaf under this top-level folder with its status.
    list_limit: cap on unregistered folders and detail rows printed.
    scan_parent: also scan the parent of storage_root for stray ``*-qptiff`` folders.
    """
    logger = get_run_logger()

    import os

    import lamindb as ln

    root = _P(storage_root)
    # Resolve the Storage record by real path: it may be registered under the mount, not the alias.
    real = os.path.realpath(storage_root)
    storage = ln.Storage.filter(root=storage_root).one_or_none()
    if storage is None:
        for s in ln.Storage.filter():
            try:
                if os.path.realpath(str(s.root)) == real:
                    storage = s
                    break
            except Exception:
                continue
    if storage is None:
        logger.warning(f"no lamindb Storage resolves to {real!r}; counting across ALL artifacts")
    else:
        logger.info(f"resolved Storage: {storage.root}")

    # Registry side: key -> uid for every Artifact in this Storage, plus which keys have bytes.
    registered: dict[str, str] = {}
    orphan_keys: list[str] = []
    arts = ln.Artifact.filter(storage=storage) if storage is not None else ln.Artifact.filter()
    for a in arts:
        if a.key is None:
            continue
        registered[a.key] = a.uid
        try:
            if not _P(str(a.path)).exists():
                orphan_keys.append(a.key)
        except Exception:
            orphan_keys.append(a.key)

    # Registered artifacts referenced by a record feature value. Best effort, one query.
    referenced: set[str] = set()
    try:
        from lamindb.models import RecordArtifact

        for rv in RecordArtifact.filter():
            v = getattr(rv, "value", None)
            if isinstance(v, ln.Artifact) and v.key is not None:
                referenced.add(v.key)
    except Exception as exc:
        logger.warning(f"record-link scan skipped: {exc}")
        referenced = None  # signal "unknown"

    # Disk side: walk each top-level folder, classify its leaves.
    per_folder: list[dict] = []
    tot_leaves = tot_reg = tot_unreg = 0
    detail_rows: list[str] = []
    for e in sorted(os.scandir(root), key=lambda x: x.name):
        name = e.name
        if name in _SKIP_TOP or name.startswith("."):
            continue
        # Raw or non-derivative files at the top level.
        if e.is_file(follow_symlinks=False) or name.endswith((".zarr", ".zarr.tar", ".qptiff", ".zip", ".tsv")):
            continue
        leaves = _leaves(_P(e.path), root)
        if not leaves:
            continue
        reg = sum(1 for k in leaves if k in registered)
        unreg = len(leaves) - reg
        per_folder.append({"folder": name, "leaves": len(leaves), "registered": reg, "unregistered": unreg})
        tot_leaves += len(leaves)
        tot_reg += reg
        tot_unreg += unreg
        if detail_prefix and name == detail_prefix:
            for k in sorted(leaves)[:list_limit]:
                status = "REGISTERED" if k in registered else "UNREGISTERED"
                if referenced is not None and k in registered and k not in referenced:
                    status = "REGISTERED (no record link)"
                detail_rows.append(f"  {status}: {k}")

    # Report.
    print(f"STORAGE ROOT: {root}", flush=True)
    print(
        f"REGISTRY: {len(registered)} artifact(s) in this storage; "
        f"{len(orphan_keys)} with MISSING bytes (orphans); "
        + (f"{len(referenced)} referenced by a record feature" if referenced is not None else "record-link scan skipped"),
        flush=True,
    )
    print(
        f"DISK: {tot_leaves} derivative leaf(s) across {len(per_folder)} folder(s): "
        f"{tot_reg} registered, {tot_unreg} UNREGISTERED (real data, no Artifact)",
        flush=True,
    )

    unreg_folders = [f for f in per_folder if f["unregistered"] > 0]
    print(f"\nFOLDERS WITH UNREGISTERED DATA: {len(unreg_folders)} (showing up to {list_limit})", flush=True)
    for f in sorted(unreg_folders, key=lambda x: -x["unregistered"])[:list_limit]:
        print(f"  {f['folder']}: {f['unregistered']}/{f['leaves']} leaf(s) unregistered", flush=True)

    # Strays outside the storage root.
    stray_folders: list[dict] = []
    stray_leaves = 0
    if scan_parent:
        parent = root.parent
        for e in sorted(os.scandir(parent), key=lambda x: x.name):
            if not e.is_dir(follow_symlinks=False) or not e.name.endswith("-qptiff"):
                continue
            if _P(e.path) == root:
                continue
            leaves = _leaves(_P(e.path), parent)  # keys relative to the PARENT (what leaked out)
            if not leaves:
                continue
            stray_folders.append({"folder": e.name, "leaves": len(leaves)})
            stray_leaves += len(leaves)
        print(
            f"\nSTRAYS OUTSIDE STORAGE ROOT ({parent}): {len(stray_folders)} folder(s), "
            f"{stray_leaves} leaf(s), real bytes that leaked out of dbstorage",
            flush=True,
        )
        for f in stray_folders[:list_limit]:
            print(f"  {f['folder']}: {f['leaves']} leaf(s)", flush=True)

    if detail_rows:
        print(f"\nDETAIL for {detail_prefix!r} (up to {list_limit}):", flush=True)
        for r in detail_rows:
            print(r, flush=True)

    logger.info(
        f"audit-storage done: leaves={tot_leaves} registered={tot_reg} unregistered={tot_unreg} "
        f"orphans={len(orphan_keys)} folders={len(per_folder)}"
    )
    return {
        "storage_root": str(root),
        "artifacts_in_storage": len(registered),
        "orphan_artifacts": len(orphan_keys),
        "disk_leaves": tot_leaves,
        "disk_registered": tot_reg,
        "disk_unregistered": tot_unreg,
        "folders_total": len(per_folder),
        "folders_with_unregistered": len(unreg_folders),
        "stray_folders_outside_root": len(stray_folders),
        "stray_leaves_outside_root": stray_leaves,
    }
