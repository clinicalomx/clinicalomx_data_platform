"""Proposing a segmentation role: a rule-based baseline here, the LLM proposer in `llm.py`.

Both consult the promoted rules before proposing, so each rejection constrains the next
proposal.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import rules as rule_engine
from ..domain.marker import MarkerPredicate
from ..seed_spec import SegmentationRole
from .schema import (
    Attention,
    Candidate,
    Proposal,
    ProposalRequest,
    Triage,
    assess,
    ranked,
)

#: Localisation substrings that suggest a channel outlines a cell.
STRUCTURAL_HINTS = ("cell membrane", "plasma membrane", "cell junction", "adherens",
                    "cytoskeleton", "basolateral", "sarcolemma")
#: Substrings meaning the channel is punctate or organelle-bound, not an outline.
ORGANELLE_HINTS = ("mitochondrion", "mitochondria", "lysosome", "peroxisome", "golgi",
                   "endoplasmic reticulum", "melanosome")
NUCLEAR_HINTS = ("nucleus", "nucleoplasm", "chromosome")


@dataclass
class RuleBasedRoleProposer:
    """Localisation to role, by the veto logic, with no model.

    Anything it cannot justify becomes `SegmentationRole.MARKER`, which excludes the channel from
    segmentation.
    """

    name: str = "rule-based"

    def propose(self, request: ProposalRequest) -> list[Proposal]:
        text = " ".join(request.locations).lower() or request.evidence.lower()
        candidates = ranked(self._score(text))
        winner, alternatives = candidates[0], candidates[1:]

        subject_row = {"canonical_name": request.marker, "compartment": text}
        try:
            rule_engine.check(MarkerPredicate.SEGMENTATION_ROLE, winner.value, subject_row)
        except ValueError as blocked:
            return [demoted(candidates, winner.value, blocked, identity=request.marker)]

        return [Proposal(predicate=MarkerPredicate.SEGMENTATION_ROLE, value=winner.value,
                         confidence=winner.confidence, reasoning=winner.reasoning,
                         alternatives=alternatives,
                         triage=assess(winner.confidence, alternatives),
                         identity=request.marker)]

    def _score(self, text: str) -> list[Candidate]:
        """Every role, with how strongly the localisation supports it.

        The veto is the absence of any structural localisation; extra locations only lower
        confidence. No structural call is confident enough to skim. ``MARKER`` on organelle evidence
        can be skimmed.

        The numbers are an uncalibrated ordinal.
        """
        structural = next((h for h in STRUCTURAL_HINTS if h in text), None)
        organelle = next((h for h in ORGANELLE_HINTS if h in text), None)
        nuclear = next((h for h in NUCLEAR_HINTS if h in text), None)
        never = Candidate(SegmentationRole.EXCLUDE, 0.0,
                          "localisation never shows a channel unusable")

        if structural:
            also = organelle or nuclear
            if also:
                return [Candidate(SegmentationRole.CYTO_STRUCTURAL, 0.65,
                                  f"localises to {structural}, so it outlines the cell"),
                        Candidate(SegmentationRole.MARKER, 0.3,
                                  f"also {also}-localised; the safe fallback"),
                        Candidate(SegmentationRole.NUCLEUS, 0.05 if nuclear else 0.0,
                                  f"{nuclear} among other locations" if nuclear
                                  else "no nuclear localisation"),
                        never]
            return [Candidate(SegmentationRole.CYTO_STRUCTURAL, 0.7,
                              f"localises to {structural}, so it outlines the cell"),
                    Candidate(SegmentationRole.MARKER, 0.3,
                              "localisation cannot show it is in every cell"),
                    Candidate(SegmentationRole.NUCLEUS, 0.0, "no nuclear localisation"),
                    never]

        if organelle:
            return [Candidate(SegmentationRole.MARKER, 0.85,
                              f"{organelle}-localised with no structural evidence"),
                    Candidate(SegmentationRole.NUCLEUS, 0.1 if nuclear else 0.0,
                              f"{nuclear} among other locations" if nuclear
                              else "no nuclear localisation"),
                    Candidate(SegmentationRole.CYTO_STRUCTURAL, 0.05,
                              "no structural localisation"),
                    never]

        if nuclear and "cytoplasm" not in text:
            return [Candidate(SegmentationRole.NUCLEUS, 0.45, "nuclear only"),
                    Candidate(SegmentationRole.MARKER, 0.4,
                              "a nuclear protein need not be in every nucleus"),
                    Candidate(SegmentationRole.CYTO_STRUCTURAL, 0.0,
                              "no structural localisation"),
                    never]

        return [Candidate(SegmentationRole.MARKER, 0.6, "no structural localisation evidence"),
                Candidate(SegmentationRole.CYTO_STRUCTURAL, 0.15,
                          "no structural localisation"),
                Candidate(SegmentationRole.NUCLEUS, 0.1 if nuclear else 0.0,
                          "nuclear and cytoplasmic" if nuclear else "no nuclear localisation"),
                never]


def demoted(candidates: tuple[Candidate, ...], blocked_value: str, blocked: ValueError, *,
            identity: str, reasoning: str = "", notes: dict | None = None) -> Proposal:
    """The ``MARKER`` fallback after a promoted rule refused ``blocked_value``.

    The blocked value is dropped from the alternatives; the rest stay ranked. Triage is always
    scrutinise.
    """
    fallback = SegmentationRole.MARKER
    stated = next((c for c in candidates if c.value == fallback), None)
    return Proposal(
        predicate=MarkerPredicate.SEGMENTATION_ROLE, value=fallback,
        confidence=stated.confidence if stated else 0.0,
        reasoning=reasoning or f"demoted: {blocked}",
        alternatives=ranked(c for c in candidates if c.value not in (fallback, blocked_value)),
        triage=Triage(Attention.SCRUTINISE,
                      f"a promoted rule blocked {str(blocked_value)!r}: {blocked}"),
        identity=identity, notes=notes or {})


def as_claim(proposal: Proposal, request: ProposalRequest, *, source: str, source_id: str,
             evidence: str, source_version: str = ""):
    """Turn a proposal into a citable claim, carrying the proposal's confidence.

    Alternatives, triage and `request.attempt` stay off the claim; the run link records which
    execution produced it.
    """
    from ..domain.marker import MarkerClaim

    if proposal.predicate == MarkerPredicate.SEGMENTATION_ROLE:
        # Raises for a value that is not a role.
        SegmentationRole(proposal.value)

    return MarkerClaim(
        subject=request.marker,
        predicate=proposal.predicate,
        value=proposal.value,
        source=source,
        source_id=source_id,
        source_version=source_version,
        evidence=evidence,
        confidence=proposal.confidence,
    )
