"""Localisation claims from a UniProt snapshot. Deterministic, no model."""

from __future__ import annotations

from ..domain import structure
from ..domain.marker import MarkerClaim, MarkerPredicate
from ..sources.snapshot import Snapshot
from ..sources.uniprot import UniProtRecord, parse


def record_from(snapshot: Snapshot) -> UniProtRecord:
    return parse(snapshot.fetched_content)


def claims_from(snapshot: Snapshot, marker: str) -> list[MarkerClaim]:
    """Localisation and GO cellular-component claims from the curated prose, fully cited.

    Unreviewed: read them through `query.claims.asserted`.
    """
    record = record_from(snapshot)
    common = {
        "subject": marker,
        "source": snapshot.source,
        "source_id": snapshot.source_id,
        "source_version": record.release,
    }

    # Every scope flattened: the claim is about the marker. Scopes stay in the snapshot and
    # `record.localisations`.
    out: list[MarkerClaim] = []
    if record.locations:
        out.append(MarkerClaim(predicate=MarkerPredicate.SUBCELLULAR_LOCATION,
                               value="; ".join(record.locations),
                               evidence=record.evidence, **common))
    # GO structures from the same curated prose via `domain.structure`; never `record.go_cc`.
    go_ids = structure.read(record).go_ids
    if go_ids:
        out.append(MarkerClaim(predicate=MarkerPredicate.GO_CC, value=" ".join(go_ids),
                               evidence=record.evidence, **common))
    return out
