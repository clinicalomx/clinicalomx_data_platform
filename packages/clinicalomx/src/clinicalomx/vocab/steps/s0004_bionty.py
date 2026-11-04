"""Pinned public ontology imports for the foundation, from ``data/sources.lock.yaml``.

Consumers pin their own on their :class:`~clinicalomx.vocab.Vocabulary`. Both reconcile through
:mod:`clinicalomx.vocab.ontology`.
"""

from __future__ import annotations

from ..dataio import sources_lock
from ..ontology import reconcile

NAME = "bionty"


def apply(rec, ctx: dict) -> None:
    out = {}
    for entity, spec in (sources_lock().get("sources") or {}).items():
        out[entity] = reconcile(rec, entity, spec)
    ctx["sources"] = out
