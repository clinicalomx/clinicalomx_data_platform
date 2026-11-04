"""reconcile-bytes: move files so each artifact's bytes sit at ``art.path``. No database writes.

Fixes bytes displaced under, or out of, a ``clinicalomx-qptiff-pipeline/`` prefix. Moves a file
only when exactly one candidate exists and the expected path is empty. Artifacts managed under
``.lamindb/<uid>`` are left alone. Ambiguous or missing cases are reported.

``dry_run=True`` (default) reports OK / FIX / MISSING / AMBIGUOUS. Runs on the HPC.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

_PIPELINE_ROOT = "clinicalomx-qptiff-pipeline"


@flow(name="reconcile-bytes", log_prints=True, retries=1, retry_delay_seconds=60)
def reconcile_bytes(
    storage_root: str,
    dry_run: bool = True,
    include_trashed: bool = False,
    limit: int | None = None,
) -> dict:
    """Move displaced bytes so each artifact's file sits at ``art.path``. No database writes.

    include_trashed: also reconcile trashed artifacts (default active only).
    limit: cap on fixes this run (None = all).
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
        raise ValueError(f"no lamindb Storage resolves to {real!r}; refusing to reconcile")
    root = Path(str(db.root))

    def _candidates(expected: Path) -> list[Path]:
        """Where ``expected``'s bytes may be: with or without the pipeline-root prefix."""
        try:
            rel = expected.relative_to(root)
        except ValueError:
            return []
        parts = rel.parts
        if parts and parts[0] == _PIPELINE_ROOT:
            return [root.joinpath(*parts[1:])]  # bytes may be at the un-prefixed path
        return [root / _PIPELINE_ROOT / rel]  # or under the pipeline-root prefix

    arts = list(ln.Artifact.filter(storage=db))
    if include_trashed:
        arts += list(ln.Artifact.filter(storage=db, branch_id=-1))

    ok = missing = ambiguous = fixed = 0
    to_fix: list[str] = []
    for a in arts:
        try:
            expected = Path(str(a.path))
        except Exception as exc:
            logger.warning(f"cannot resolve path for {a.uid} ({a.key}): {exc}")
            continue
        if expected.exists():
            ok += 1
            continue
        found = [c for c in _candidates(expected) if c.exists()]
        if len(found) == 1:
            src = found[0]
            print(f"{'PLAN-FIX' if dry_run else 'FIX'}: {a.key}\n    <- {src}\n    -> {expected}", flush=True)
            to_fix.append(a.key or a.uid)
            if not dry_run and (limit is None or fixed < limit):
                expected.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(expected))  # bytes only; the database is unchanged
                fixed += 1
        elif not found:
            print(f"MISSING (no bytes at {expected} or known variant): {a.key}", flush=True)
            missing += 1
        else:
            print(f"AMBIGUOUS (multiple candidates, skipping): {a.key}", flush=True)
            ambiguous += 1

    logger.info(
        f"reconcile-bytes done: scanned={len(arts)} ok={ok} to_fix={len(to_fix)} fixed={fixed} "
        f"missing={missing} ambiguous={ambiguous} dry_run={dry_run}"
    )
    return {
        "scanned": len(arts), "ok": ok, "to_fix": len(to_fix), "fixed": fixed,
        "missing": missing, "ambiguous": ambiguous, "dry_run": dry_run,
    }
