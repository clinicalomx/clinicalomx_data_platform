"""Ledger writes, and the answer and queue reads.

A question is (domain, subject, predicate). Every read and write is scoped by domain.
The answer is each claim whose newest decision accepts it, minus voided sources.
The queue is the standing claims with no decision, most confident first.
Rows are append-only, so nothing locks. The schema refuses invalid rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from django.db import transaction

from .grammar.published import Answer, Published, manifest_hash, render
from .models import (
    Claim,
    Decision,
    Outcome,
    Policy,
    Release,
    ReleaseEntry,
    SourceInvalidationEvent,
)

#: Oldest first; the autoincrement pk breaks timestamp ties.
CREATION_ORDER = ("created_at", "id")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _voided(domain: str):
    """A predicate over claims: whether a void in ``domain`` covers one."""
    voids = list(SourceInvalidationEvent.objects.filter(domain=domain))

    def voided(claim: Claim) -> bool:
        return any(
            v.source == claim.source
            and v.source_id == claim.source_id
            and v.source_version in ("", claim.source_version)
            for v in voids
        )

    return voided


def _question(domain: str, subject: str | None = None, predicate: str | None = None):
    rows = Claim.objects.filter(domain=domain)
    if subject is not None:
        rows = rows.filter(subject=subject)
    if predicate is not None:
        rows = rows.filter(predicate=predicate)
    return rows


def identity(claim: Claim) -> tuple[str, ...]:
    """The columns that make two rows one claim: `grammar.model.Claim.claim_id` as columns."""
    return (claim.subject, claim.predicate, claim.source, claim.source_id, claim.value,
            claim.source_version)


def newest_decision(claim: Claim) -> Decision | None:
    """The decision ``claim`` is ruled by now, on any of its rows."""
    return _in_effect(claim.domain, claim.subject, claim.predicate).get(identity(claim))


def _slot(claim: Claim, multi_valued: Collection[str]) -> tuple[str, ...]:
    """`grammar.model.supersession_key` as columns. Keep the two in step."""
    return (claim.subject, claim.predicate, claim.source, claim.source_id,
            claim.value if claim.predicate in multi_valued else "")


def standing(domain: str, subject: str, predicate: str, *,
             multi_valued: Collection[str] = ()) -> list[Claim]:
    """Newest claim per (source, source_id), voids applied before supersession.

    On a ``multi_valued`` predicate each value is its own slot.
    """
    voided = _voided(domain)
    kept: dict[tuple[str, ...], Claim] = {}
    for claim in _question(domain, subject, predicate).order_by(*CREATION_ORDER):
        if not voided(claim):
            kept[_slot(claim, multi_valued)] = claim
    return list(kept.values())


def _in_effect(domain: str, subject: str | None = None,
               predicate: str | None = None) -> dict[tuple[str, ...], Decision]:
    """The newest decision per claim identity, each carrying its claim."""
    newest: dict[tuple[str, ...], Decision] = {}
    rows = (Decision.objects.select_related("claim")
            .filter(claim__in=_question(domain, subject, predicate))
            .order_by(*CREATION_ORDER))
    for decision in rows:
        newest[identity(decision.claim)] = decision
    return newest


def answers(domain: str, subject: str | None = None,
            predicate: str | None = None) -> list[Decision]:
    """Accepted decisions in effect on unvoided claims, oldest first, each with its claim.

    Narrowed by subject and predicate, or the whole domain when neither is given.
    """
    voided = _voided(domain)
    return sorted(
        (d for d in _in_effect(domain, subject, predicate).values()
         if d.outcome == Outcome.ACCEPTED and not voided(d.claim)),
        key=lambda d: (d.created_at, d.id),
    )


def queue(domain: str, *, multi_valued: Collection[str] = ()) -> list[Claim]:
    """Standing claims with no decision, most confident first. No confidence sorts last."""
    voided = _voided(domain)
    decided = set(_in_effect(domain))
    kept: dict[tuple[str, ...], Claim] = {}
    for claim in _question(domain).order_by(*CREATION_ORDER):
        if not voided(claim):
            kept[_slot(claim, multi_valued)] = claim
    pending = [c for c in kept.values() if identity(c) not in decided]
    return sorted(pending, key=lambda c: (c.confidence is None, -(c.confidence or 0.0),
                                          c.created_at, c.id))


@transaction.atomic
def create_claim(
    *,
    domain: str,
    subject: str,
    predicate: str,
    value: str,
    source: str,
    source_id: str,
    source_version: str = "",
    evidence: str,
    proposed_by: str,
    actor_kind: str,
    agent_model: str = "",
    agent_model_version: str = "",
    confidence: float | None = None,
    policy_version: str = "",
    run_uid: str | None = None,
    multi_valued: Collection[str] = (),
) -> Claim:
    """Append one claim. A repeat of the source's standing value and release is a no-op.

    ``confidence`` is not part of the comparison.
    """
    for held in standing(domain, subject, predicate, multi_valued=multi_valued):
        if (held.source, held.source_id, held.value, held.source_version) == (
            source, source_id, value, source_version
        ):
            return held
    return Claim.objects.create(
        uid=_uid("clm"),
        domain=domain,
        subject=subject,
        predicate=predicate,
        value=value,
        source=source,
        source_id=source_id,
        source_version=source_version,
        evidence=evidence,
        proposed_by=proposed_by,
        actor_kind=actor_kind,
        agent_model=agent_model,
        agent_model_version=agent_model_version,
        confidence=confidence,
        policy_version=policy_version,
        run_uid=run_uid,
    )


@transaction.atomic
def decide(
    *,
    claim: Claim,
    outcome: str,
    by: str,
    actor_kind: str,
    decided_at: datetime,
    kind: str = "",
    reason: str = "",
    agent_model: str = "",
    agent_model_version: str = "",
    policy_version: str | None = None,
    run_uid: str | None = None,
) -> Decision | None:
    """Append one decision on ``claim``, or no-op when its newest decision says the same.

    Compares outcome, kind, reason, decider and ``decided_at``, not who recorded it.
    """
    head = newest_decision(claim)
    if head is not None and (
        head.outcome, head.kind, head.reason, head.by, head.decided_at
    ) == (outcome, kind, reason, by, decided_at):
        return None
    return Decision.objects.create(
        uid=_uid("dec"),
        claim=claim,
        outcome=outcome,
        kind=kind,
        reason=reason,
        by=by,
        actor_kind=actor_kind,
        agent_model=agent_model,
        agent_model_version=agent_model_version,
        policy_id=policy_version,
        decided_at=decided_at,
        run_uid=run_uid,
    )


def approve_policy(*, version: str, approved_by: str, note: str = "") -> Policy:
    """Record a human's approval of a policy an agent may then decide under."""
    return Policy.objects.create(version=version, approved_by=approved_by, note=note)


@transaction.atomic
def cut_release(
    *,
    domain: str,
    by: str,
    actor_kind: str,
    agent_model: str = "",
    agent_model_version: str = "",
    policy_version: str = "",
    run_uid: str | None = None,
) -> Release:
    """Pin the decisions behind ``domain``'s current answers."""
    pinned = answers(domain)
    release = Release.objects.create(
        uid=_uid("rel"),
        domain=domain,
        manifest_hash=manifest_hash(d.uid for d in pinned),
        policy_version=policy_version,
        by=by,
        actor_kind=actor_kind,
        agent_model=agent_model,
        agent_model_version=agent_model_version,
        run_uid=run_uid,
    )
    ReleaseEntry.objects.bulk_create(
        ReleaseEntry(release=release, decision=d) for d in pinned)
    return release


def release_contents(uid: str) -> list[Decision]:
    """The decisions a release pinned, oldest first."""
    return list(Decision.objects.select_related("claim")
                .filter(id__in=ReleaseEntry.objects.filter(release__uid=uid)
                        .values("decision_id"))
                .order_by(*CREATION_ORDER))


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def export_release(uid: str) -> bytes:
    """The release ``uid`` as the bytes `grammar.published.load` reads. Always the same bytes."""
    release = Release.objects.get(uid=uid)
    return render(Published(
        release_uid=release.uid,
        domain=release.domain,
        manifest_hash=release.manifest_hash,
        released_at=_utc(release.created_at),
        released_by=release.by,
        policy_version=release.policy_version,
        answers=tuple(
            Answer(
                subject=d.claim.subject,
                predicate=d.claim.predicate,
                value=d.claim.value,
                source=d.claim.source,
                source_id=d.claim.source_id,
                source_version=d.claim.source_version,
                evidence=d.claim.evidence,
                claim_uid=d.claim.uid,
                decision_uid=d.uid,
                decided_by=d.by,
                decided_at=_utc(d.decided_at),
            )
            for d in release_contents(uid)
        ),
    ))


@transaction.atomic
def invalidate_source(
    *,
    domain: str,
    source: str,
    source_id: str,
    source_version: str = "",
    reason: str,
    actor: str,
    actor_kind: str,
    agent_model: str = "",
    agent_model_version: str = "",
    note: str = "",
    occurred_at: datetime | None = None,
) -> SourceInvalidationEvent:
    """Append one void. ``occurred_at`` is the stated date; ``created_at`` the append time."""
    return SourceInvalidationEvent.objects.create(
        uid=_uid("inv"),
        domain=domain,
        source=source,
        source_id=source_id,
        source_version=source_version,
        reason=reason,
        note=note,
        actor=actor,
        actor_kind=actor_kind,
        agent_model=agent_model,
        agent_model_version=agent_model_version,
        occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
    )
