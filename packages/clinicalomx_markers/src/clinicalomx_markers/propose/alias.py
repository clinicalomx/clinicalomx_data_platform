"""Proposing an alias: which marker a spelling names, read from UniProt's synonyms. No model.

An alias is an ordinary ``(marker, alias, spelling)`` claim, accepted or rejected by a person.
``alias`` is multi-valued, so one accession can list several.

The request's ``marker`` is the spelling and each proposal's ``value`` is a marker. A spelling
several markers list is proposed for each, confidence split, with the others as
alternatives. `as_claim` turns a proposal into the claim: subject the marker, value the
spelling. A person proposes through `human.HumanProposer.answer` with ``predicate=ALIAS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.marker import MarkerClaim, MarkerPredicate, MarkerSource
from ..resolve import match_key
from ..sources.uniprot import UniProtRecord
from .schema import Candidate, Proposal, ProposalRequest, assess, ranked

#: Confidence of a listed UniProt synonym before any collision splits it.
#: An uncalibrated ordinal, as in `role.py`.
SYNONYM_CONFIDENCE = 0.9


def spellings(record: UniProtRecord) -> tuple[tuple[str, str], ...]:
    """``(spelling, what UniProt calls it)`` for every gene synonym and alternative protein name.

    In UniProt's order; a spelling in both lists is kept once.
    """
    listed = [(s, "gene synonym") for s in record.gene_synonyms]
    listed += [(s, "alternative name") for s in record.alternative_names]
    seen: dict[str, tuple[str, str]] = {}
    for spelling, kind in listed:
        seen.setdefault(match_key(spelling), (spelling, kind))
    return tuple(seen.values())


@dataclass
class UniProtAliasProposer:
    """Spelling to marker, from the already-fetched UniProt ``records`` of known markers."""

    records: Mapping[str, UniProtRecord]
    name: str = "uniprot-synonyms"

    def _listing(self, spelling: str) -> list[str]:
        """The markers whose entry lists ``spelling``."""
        key = match_key(spelling)
        return sorted(marker for marker, record in self.records.items()
                      if key != match_key(marker)
                      and any(match_key(s) == key for s, _ in spellings(record)))

    def propose(self, request: ProposalRequest) -> list[Proposal]:
        """One proposal per marker listing the spelling, each ranking the others behind it."""
        listing = self._listing(request.marker)
        if not listing:
            return []
        share = SYNONYM_CONFIDENCE / len(listing)
        proposals = []
        for marker in listing:
            rivals = ranked(Candidate(other, share, f"{other}'s UniProt entry lists it too")
                            for other in listing if other != marker)
            proposals.append(Proposal(
                predicate=MarkerPredicate.ALIAS, value=marker, confidence=share,
                reasoning=f"{marker}'s UniProt entry lists {request.marker!r}",
                alternatives=rivals, triage=assess(share, rivals), identity=request.marker))
        return proposals


def as_claim(proposal: Proposal, request: ProposalRequest, *, source: MarkerSource | str,
             source_id: str, evidence: str, source_version: str = "") -> MarkerClaim:
    """The alias claim a proposal makes: about the proposed marker, asserting the spelling."""
    if proposal.predicate != MarkerPredicate.ALIAS:
        raise ValueError(f"{proposal.predicate!r} is not an alias proposal")
    return MarkerClaim(subject=proposal.value, predicate=MarkerPredicate.ALIAS,
                       value=request.marker, source=source, source_id=source_id,
                       source_version=source_version, evidence=evidence,
                       confidence=proposal.confidence)


def seed_claims(records: Mapping[str, UniProtRecord]) -> list[MarkerClaim]:
    """Every UniProt synonym of every marker in ``records``, as an undecided alias claim cited to
    the entry and release."""
    proposer = UniProtAliasProposer(records)
    claims = []
    for marker, record in records.items():
        for spelling, kind in spellings(record):
            request = ProposalRequest(marker=spelling, evidence=f"{kind}: {spelling}")
            for proposal in proposer.propose(request):
                if proposal.value == marker:
                    claims.append(as_claim(proposal, request, source=MarkerSource.UNIPROT,
                                           source_id=record.accession,
                                           source_version=record.release,
                                           evidence=request.evidence))
    return claims
