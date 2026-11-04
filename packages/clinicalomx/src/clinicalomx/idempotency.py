"""Idempotent artifact materialisation for lamindb pipelines.

:func:`materialize` skips the compute when every output exists with its bytes, and always runs
``reconcile``, so metadata, feature, ROI and lineage writes are never skipped.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Literal

Heal = Literal["delete", "repoint", "error"]


def existing_artifact(key: str, *, heal: Heal = "delete", is_latest: bool = True):
    """The live Artifact for ``key`` if it exists and its bytes are on disk, else ``None``.

    ``heal`` handles a record whose bytes are missing:

    - ``"delete"`` (default): drop the record (``permanent=True, storage=False``) and return
      ``None``, so the caller recomputes.
    - ``"repoint"``: keep the record and return ``None``; the recompute's ``.save()`` to the same
      key revises it.
    - ``"error"``: raise.
    """
    import lamindb as ln
    rec = ln.Artifact.filter(key=key, is_latest=is_latest).one_or_none()
    if rec is None:
        return None
    try:
        present = rec.path.exists()
    except Exception:
        present = False
    if present:
        return rec
    if heal == "error":
        raise FileNotFoundError(f"artifact {key!r}: record exists but its bytes are missing")
    if heal == "delete":
        print(f"  stale record, bytes missing -> recompute: {key}")
        try:
            rec.delete(permanent=True, storage=False)
        except Exception as e:
            print(f"    warning: could not delete stale record: {type(e).__name__}: {e}")
    else:  # repoint: the recompute revises the same key
        print(f"  stale record, bytes missing -> repoint on recompute: {key}")
    return None


def materialize(
    keys: Iterable[str],
    *,
    compute: Callable[[], Mapping[str, object]],
    reconcile: Callable[[Mapping[str, object]], None] | None = None,
    heal: Heal = "delete",
) -> dict:
    """Skip ``compute`` when every output exists; always run ``reconcile``.

    ``compute`` writes and registers the outputs and returns ``{key: Artifact}``; it runs only on
    a miss. ``reconcile`` receives that mapping on both paths and must be idempotent. ``heal`` is
    passed to :func:`existing_artifact`.
    """
    keys = list(keys)
    existing = {k: existing_artifact(k, heal=heal) for k in keys}
    if all(v is not None for v in existing.values()):
        print(f"  skip compute (exists): {', '.join(keys)}")
        arts = existing
    else:
        arts = dict(compute())
    if reconcile is not None:
        reconcile(arts)
    return arts
