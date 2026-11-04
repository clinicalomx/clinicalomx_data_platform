"""The human-in-the-loop gate: propose, accept, reject, retry.

    claim written -> unresolved -> accepted
                          |
                          +------> rejected   (kind: rule | hint | one_off)
                                      retry: a new claim, which is unresolved again

A claim with no decision is unresolved. The newest decision on a claim is in effect.
Each domain keeps its own review queue on its own state feature.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .model import (
    Claim,
    ClaimDomain,
    Decision,
    DecisionKind,
    Outcome,
    in_effect,
    short_id,
)


class ReviewState(StrEnum):
    """Review lifecycle of a claim subject. Held under `ClaimDomain.state_feature`.

    ``UNRESOLVED`` means nobody has ruled. The other members match `Outcome`.
    """

    UNRESOLVED = "unresolved"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Judgement(BaseModel):
    """A human ruling before it is bound to a claim. The store turns it into `Decision` records.

    ``by`` and ``at`` are mandatory and refused blank. ``kind`` and ``reason`` are optional.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str
    at: str
    kind: DecisionKind | Literal[""] = ""
    reason: str = ""

    @model_validator(mode="after")
    def _a_judgement_names_its_human(self) -> Judgement:
        if not self.by:
            raise ValueError("a judgement needs the human who made it: by is blank")
        if not self.at:
            raise ValueError("a judgement needs the date it was made: at is blank")
        return self


def decisions_for(
    domain: ClaimDomain,
    claim: Claim,
    *,
    outcome: Outcome | None,
    decisions: Sequence[Decision],
    judgement: Judgement | None = None,
) -> list[Decision]:
    """The decisions this write records on ``claim``, possibly none.

    ``outcome`` None only proposes and writes no decision. Otherwise ``judgement`` is required.
    A ruling the claim's newest decision already says is not repeated. A ruling on a predicate
    the domain does not review raises.
    """
    if claim.predicate not in domain.reviewed_predicates:
        if outcome is not None:
            raise ValueError(
                f"{claim.subject}: predicate {str(claim.predicate)!r} is not reviewed in this "
                f"domain, so there is nowhere to record {str(outcome)!r} or who decided it. "
                f"Only {domain.reviewed_predicates} take a decision"
            )
        return []
    if outcome is None:
        return []
    if judgement is None:
        raise ValueError(f"{claim.subject}: {str(outcome)!r} decides something, so it needs "
                         "a judgement saying who decided it and when")
    decided = domain.decision(
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        claim_id=short_id(claim.claim_id()),
        outcome=outcome,
        kind=judgement.kind,
        reason=judgement.reason,
        by=judgement.by,
        at=judgement.at,
    )
    standing = in_effect(decisions).get(decided.claim_id)
    if standing is not None and standing.decision() == decided.decision():
        return []
    return [decided]


def subject_state_for(domain: ClaimDomain, claims: Sequence[Claim],
                      decisions: Sequence[Decision]) -> ReviewState | None:
    """The review state a subject's standing claims and decisions imply, or None.

    Each reviewed predicate is read on its own. The subject takes its least-advanced predicate:
    unresolved, then rejected, then accepted. None when no reviewed predicate has a claim.
    ``decisions`` must be oldest first.
    """
    effect = in_effect(decisions)
    per_predicate: dict[str, list[Decision | None]] = {}
    for claim in claims:
        if claim.predicate in domain.reviewed_predicates:
            per_predicate.setdefault(str(claim.predicate), []).append(
                effect.get(short_id(claim.claim_id())))
    if not per_predicate:
        return None
    states = set()
    for ruled in per_predicate.values():
        if any(d is None for d in ruled):
            states.add(ReviewState.UNRESOLVED)
        elif any(d.outcome == Outcome.ACCEPTED for d in ruled):
            states.add(ReviewState.ACCEPTED)
        else:
            states.add(ReviewState.REJECTED)
    return next(s for s in (ReviewState.UNRESOLVED, ReviewState.REJECTED, ReviewState.ACCEPTED)
                if s in states)


def set_subject_state(record, state: str, *, feature: str):
    """Set a claim subject's review state on the domain's ``feature``."""
    from clinicalomx.review import set_state

    return set_state(record, ReviewState(state).value, feature=feature)


def subjects_in_state(state: str, *, branch, types: tuple[str, ...], feature: str):
    """Queryset of claim subjects in ``state``, filtered in SQL.

    Every filter call names the branch: lamindb scopes a call with no branch to main plus the
    ambient branch.
    """
    import lamindb as ln

    return ln.Record.filter(type__name__in=types, branch=branch).filter(
        values_json__feature__name=feature,
        values_json__value=ReviewState(state).value,
        branch=branch,
    )
