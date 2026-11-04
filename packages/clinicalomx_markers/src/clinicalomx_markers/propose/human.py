"""A person proposing, through the same `Proposer` protocol as the rule-based and LLM proposers.

`curate/` accepts either unchanged. The ledger records who acted through the store session's
`actor_kind`. Imports nothing from the agent stack. An API, not a UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain import rules as rule_engine
from ..domain.marker import MarkerPredicate
from .schema import Candidate, Proposal, ProposalRequest, Reason, assess, ranked


@dataclass
class HumanProposer:
    """One person's answers, replayed through the `Proposer` protocol.

    ``by`` names the person, has no default, and is never read from the environment. Pass it to
    the store as ``LedgerClaimStore(..., actor=proposer.by, actor_kind="human")``. `answer`
    records an answer; `propose` returns them.
    """

    by: str
    name: str = "human"
    #: Subject -> this person's answers, oldest first. Several answers are several proposals.
    answers: dict[str, list[Proposal]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by.strip():
            raise ValueError("a human proposal names the person making it; `by` is blank")

    def answer(self, request: ProposalRequest, value: str, *, confidence: float,
               reasoning: str = "", alternatives: tuple[Candidate, ...] = (),
               predicate: str = MarkerPredicate.SEGMENTATION_ROLE,
               reasons: tuple[Reason, ...] = (), transform: str = "") -> Proposal:
        """Record this person's answer to ``request`` as a Proposal.

        ``confidence`` has no default. ``alternatives`` are ranked by their confidences, and
        triage is `schema.assess`. ``reasons`` are optional. ``transform`` is as in
        `schema.Proposal.transform`.
        The same predicate, value and transform replaces an earlier answer; anything else adds one.

        Raises when the request has no evidence, or when a promoted rule forbids the answer.
        """
        if not request.evidence.strip():
            raise ValueError(
                f"{request.marker}: nothing is cited, so the claim would not be citable. "
                "A human proposal quotes the upstream text it rests on, exactly as a "
                "machine one does"
            )
        rule_engine.check(predicate, value, {"canonical_name": request.marker,
                                             "compartment": " ".join(request.locations)})
        alternatives = ranked(alternatives)
        proposal = Proposal(predicate=predicate, value=value, confidence=confidence,
                            reasoning=reasoning, alternatives=alternatives, reasons=reasons,
                            triage=assess(confidence, alternatives), identity=request.marker,
                            transform=transform)
        same = (predicate, value, transform)
        kept = [p for p in self.answers.get(request.marker, [])
                if (p.predicate, p.value, p.transform) != same]
        self.answers[request.marker] = [*kept, proposal]
        return proposal

    def propose(self, request: ProposalRequest) -> list[Proposal]:
        """This person's answers for ``request``, or nothing."""
        return list(self.answers.get(request.marker, []))
