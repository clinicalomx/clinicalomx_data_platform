"""Read and cite what the lab has accepted and what its sources say. No model, no network.

`settled` answers what the lab has accepted: every claim whose newest decision accepts it.
`asserted` answers what the sources say: every standing claim, rivals side by side. An
undecided claim is never in `settled`.

Decisions are append-only. A rejection takes its claim out of the answer; nothing falls back
to an earlier claim. A citation renders a decision with the claim it rules on.

Every function takes rows as parameters, oldest first, as every store's `held` and `answers`
return them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from clinicalomx_ledger.grammar.lineage import Lineage
from clinicalomx_ledger.grammar.model import Claim, Decision, Outcome, short_id


def settled(
    answers: list[Decision],
    subject: str,
    *,
    predicate: str | None = None,
) -> list[Decision]:
    """The accepted decisions in effect about ``subject``, oldest first.

    Pass a store's ``answers(subject)``. This narrows it to one predicate.
    """
    return [d for d in answers
            if d.subject == subject and (predicate is None or d.predicate == predicate)]


def value_of(
    answers: list[Decision],
    subject: str,
    predicate: str,
) -> Decision | None:
    """The newest accepted decision, or None. ``answers`` must be in creation order."""
    found = settled(answers, subject, predicate=predicate)
    return found[-1] if found else None


def asserted(claims: list[Claim], subject: str) -> dict[str, tuple[Claim, ...]]:
    """What the sources say about ``subject``: predicate -> every standing claim, newest first.

    Pass a store's ``held(subject)``. A predicate nobody asserted is absent. This is how an
    unreviewed predicate such as localisation is read.
    """
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        if claim.subject == subject:
            grouped.setdefault(claim.predicate, []).append(claim)
    return {p: tuple(reversed(held)) for p, held in grouped.items()}


def supporting(decision: Decision, claims: list[Claim]) -> list[Claim]:
    """The claim a decision rules on, as a list of at most one.

    Pass ``store.held(subject, history=True)``, not the projection: the claim may have been
    superseded since the decision. Matched by `short_id`. A missing claim gives an empty list.
    Two claims sharing a `claim_id` resolve to the newest.
    """
    return [c for c in claims if short_id(c.claim_id()) == decision.claim_id][-1:]


def marker(claim: Claim) -> str:
    """Short inline marker, e.g. ``[NAKATPASE:segmentation_role:uniprot:P05023:7e63416ddb18]``."""
    return f"[{claim.claim_id()}]"


def line(
    decision: Decision,
    claims: Sequence[Claim] = (),
    lineage: Callable[[Claim], Lineage | None] | None = None,
) -> str:
    """One citation for a decision: what stands, who decided it, and on what authority.

    A rejection is printed in capitals. ``claims`` is what `supporting` selects; empty renders
    the decision alone. ``lineage`` is `ClaimStore.lineage`; omitted, no run clause is printed.
    """
    trusted = decision.outcome == Outcome.ACCEPTED
    status = str(decision.outcome) if trusted else f"{decision.outcome.upper()} UNVALIDATED"
    who = " ".join(p for p in (decision.by, decision.at) if p)
    head = (f"{decision.subject} {decision.predicate} = {decision.value!r}\n"
            f"    {status}{'  ' + who if who else ''}")
    return "\n".join([head] + [f"    supported by {_source(c, lineage)}" for c in claims])


def claim_line(claim: Claim, lineage: Lineage | None = None) -> str:
    """One citation for a claim with no decision, marked UNDECIDED."""
    return (f"{claim.subject} {claim.predicate} = {claim.value!r}\n"
            f"    UNDECIDED  {_source(claim, lambda _: lineage)}")


def _source(claim: Claim, lineage: Callable[[Claim], Lineage | None] | None) -> str:
    """One claim as a pointer a reader can follow: the marker, where it came from, and the run."""
    where = f"{claim.source} {claim.source_id}".strip()
    if claim.source_version:
        where += f" @{claim.source_version}"
    return f"{marker(claim)} {where}{_computed(lineage(claim) if lineage else None)}"


def _computed(lineage: Lineage | None) -> str:
    """The run clause, or nothing at all when there is no run."""
    if lineage is None:
        return ""
    parts = [f"run {lineage.run_uid}"] if lineage.run_uid else []
    if lineage.started_at:
        parts.append(f"started {lineage.started_at}")
    return f"  {'  '.join(parts)}" if parts else ""


def block(
    decisions: Sequence[Decision],
    claims: Sequence[Claim] = (),
    lineage: Callable[[Claim], Lineage | None] | None = None,
) -> str:
    """Citations for a set of decisions, ordered by subject, predicate and value.

    Pass ``store.held(subject, history=True)`` as ``claims``, as for `supporting`.
    """
    ordered = sorted(decisions, key=lambda v: (v.subject, v.predicate, v.value))
    return "\n".join(line(v, supporting(v, list(claims)), lineage) for v in ordered)
