"""The vocabulary seed steps, in order.

Each step is small and idempotent, and exposes ``NAME`` and ``apply(rec)`` taking a
:class:`~clinicalomx.vocab.upsert.Reconciler`. State a later step needs is re-resolved through
``rec``.

Only the foundation lives here. A consumer declares through the ``clinicalomx.vocabulary`` entry
point and adds no step. Never float an ontology source to ``latest-version``.
"""

from __future__ import annotations

from . import (
    s0001_storage,
    s0002_types,
    s0003_features,
    s0004_bionty,
)

#: Applied in order. Append; never reorder or renumber a step that has run.
STEPS = [
    s0001_storage,
    s0002_types,
    s0003_features,
    s0004_bionty,
]

__all__ = ["STEPS"]
