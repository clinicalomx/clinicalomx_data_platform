"""Reading an Artifact-valued feature in any of its shapes.

A value is an ``ln.Artifact``, its key as a string, or a list or set of either (role features are
multi-valued, and one value comes back as a scalar). :func:`newest_artifact` gives the current
result; :func:`all_artifacts` gives every version. lamindb is imported inside the functions.
"""
from __future__ import annotations

from typing import Any

_MANY = (list, tuple, set, frozenset)


def as_artifact(value: Any):
    """One value (an Artifact, or a key string) -> Artifact, or ``None``.

    A key string resolves to the newest Artifact with that key.
    """
    import lamindb as ln

    if value is None or isinstance(value, ln.Artifact):
        return value
    return ln.Artifact.filter(key=str(value)).order_by("-created_at").first()


def all_artifacts(value: Any) -> list:
    """Every Artifact in an Artifact-valued feature. Unresolvable values are dropped."""
    values = list(value) if isinstance(value, _MANY) else [value]
    return [a for a in (as_artifact(v) for v in values) if a is not None]


def newest_artifact(value: Any):
    """The newest Artifact (by ``created_at``) in an Artifact-valued feature, or ``None``."""
    arts = all_artifacts(value)
    if not arts:
        return None
    if all(getattr(a, "created_at", None) is not None for a in arts):
        return max(arts, key=lambda a: a.created_at)
    return arts[-1]
