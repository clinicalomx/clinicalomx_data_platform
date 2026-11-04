"""Declarative vocabulary for the clinicalomx LaminDB instance.

    plan    what would change (no writes)
    apply   converge the instance toward the declared state
    check   exit non-zero on any drift          -> CI gate
    audit   features on the instance the repo does not declare

Every write goes through :mod:`.upsert`. A declared dtype is a registry class or its canonical
string: ``bt.Protein`` and ``"cat[bionty.Protein]"`` are the same; a bare ``"Record"`` raises.
When two repos declare one feature, :func:`owners` keeps the first it finds.
"""

from ..names import ALL_FEATURES, ALL_TYPES, FEATURES, PROJECT, TYPES
from .registry import ENTRY_POINT_GROUP, FOUNDATION, Declared, Vocabulary, discover, owners


def __getattr__(name: str):
    """Import ``Change`` and ``Reconciler`` lazily: they need lamindb, which connects."""
    if name in {"Change", "Reconciler"}:
        from . import upsert

        return getattr(upsert, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Vocabulary",
    "Declared",
    "discover",
    "owners",
    "ENTRY_POINT_GROUP",
    "FOUNDATION",
    "ALL_FEATURES",
    "ALL_TYPES",
    "Change",
    "FEATURES",
    "PROJECT",
    "Reconciler",
    "TYPES",
]
