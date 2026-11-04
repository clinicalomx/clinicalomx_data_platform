"""Ledger reads for a consumer that holds no claim store.

Five reads: resolved state, decision trail, standing claims, by run, by source.
Subject-scoped reads take the domain first. `by_run` and `by_source` cross domains.
Rows come back as frozen dataclasses, never ORM instances. Rows with ``schema_version`` 1
have no confidence and no policy.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from . import services
from .models import Claim as ClaimRow
from .models import Decision as DecisionRow
from .models import SourceInvalidationEvent


@dataclass(frozen=True)
class Resolved:
    """One claim that is currently the answer, and the decision that made it so."""

    domain: str
    subject: str
    predicate: str
    value: str
    claim_uid: str
    decision_uid: str
    decided_by: str
    decided_at: datetime


@dataclass(frozen=True)
class Claim:
    uid: str
    domain: str
    subject: str
    predicate: str
    value: str
    source: str
    source_id: str
    source_version: str
    evidence: str
    proposed_by: str
    actor_kind: str
    agent_model: str
    agent_model_version: str
    confidence: float | None
    policy_version: str
    run_uid: str | None
    schema_version: int
    created_at: datetime


@dataclass(frozen=True)
class Decision:
    """One ruling on one claim. Subject, predicate and value are the claim's."""

    uid: str
    claim_uid: str
    domain: str
    subject: str
    predicate: str
    value: str
    outcome: str
    kind: str
    reason: str
    by: str
    actor_kind: str
    agent_model: str
    agent_model_version: str
    policy_version: str | None
    decided_at: datetime
    run_uid: str | None
    schema_version: int
    created_at: datetime


@dataclass(frozen=True)
class Invalidation:
    uid: str
    domain: str
    source: str
    source_id: str
    source_version: str
    reason: str
    actor: str
    actor_kind: str
    occurred_at: datetime
    created_at: datetime


def _claim(row: ClaimRow) -> Claim:
    return Claim(
        uid=row.uid,
        domain=row.domain,
        subject=row.subject,
        predicate=row.predicate,
        value=row.value,
        source=row.source,
        source_id=row.source_id,
        source_version=row.source_version,
        evidence=row.evidence,
        proposed_by=row.proposed_by,
        actor_kind=row.actor_kind,
        agent_model=row.agent_model,
        agent_model_version=row.agent_model_version,
        confidence=row.confidence,
        policy_version=row.policy_version,
        run_uid=row.run_uid,
        schema_version=row.schema_version,
        created_at=row.created_at,
    )


def _decision(row: DecisionRow) -> Decision:
    claim = row.claim
    return Decision(
        uid=row.uid,
        claim_uid=claim.uid,
        domain=claim.domain,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        outcome=row.outcome,
        kind=row.kind,
        reason=row.reason,
        by=row.by,
        actor_kind=row.actor_kind,
        agent_model=row.agent_model,
        agent_model_version=row.agent_model_version,
        policy_version=row.policy_id,
        decided_at=row.decided_at,
        run_uid=row.run_uid,
        schema_version=row.schema_version,
        created_at=row.created_at,
    )


def _invalidation(row: SourceInvalidationEvent) -> Invalidation:
    return Invalidation(
        uid=row.uid,
        domain=row.domain,
        source=row.source,
        source_id=row.source_id,
        source_version=row.source_version,
        reason=row.reason,
        actor=row.actor,
        actor_kind=row.actor_kind,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def resolved_state(domain: str, subject: str, predicate: str) -> tuple[Resolved, ...]:
    """Every claim currently the answer to a question, oldest decision first."""
    return tuple(
        Resolved(
            domain=d.claim.domain,
            subject=d.claim.subject,
            predicate=d.claim.predicate,
            value=d.claim.value,
            claim_uid=d.claim.uid,
            decision_uid=d.uid,
            decided_by=d.by,
            decided_at=d.decided_at,
        )
        for d in services.answers(domain, subject, predicate)
    )


def decision_trail(domain: str, subject: str,
                   predicate: str | None = None) -> tuple[Decision, ...]:
    """Every decision on a subject's claims in event order, superseded ones included."""
    rows = DecisionRow.objects.select_related("claim").filter(
        claim__domain=domain, claim__subject=subject)
    if predicate is not None:
        rows = rows.filter(claim__predicate=predicate)
    return tuple(_decision(d) for d in rows.order_by(*services.CREATION_ORDER))


def standing_claims(domain: str, subject: str, predicate: str, *,
                    multi_valued: Collection[str] = ()) -> tuple[Claim, ...]:
    """Unvoided newest claim per (source, source_id), as `services.standing`."""
    return tuple(_claim(c) for c in services.standing(domain, subject, predicate,
                                                      multi_valued=multi_valued))


def by_run(run_uid: str) -> tuple[Claim | Decision, ...]:
    """Every claim and decision citing a lamindb run uid, in event order."""
    events: list[tuple[datetime, Claim | Decision]] = [
        (c.created_at, _claim(c)) for c in ClaimRow.objects.filter(run_uid=run_uid)
    ]
    events += [
        (d.created_at, _decision(d))
        for d in DecisionRow.objects.select_related("claim").filter(run_uid=run_uid)
    ]
    return tuple(event for _, event in sorted(events, key=lambda pair: pair[0]))


def by_source(
    source: str, source_id: str, source_version: str | None = None
) -> tuple[Claim | Invalidation, ...]:
    """Every claim from a source plus every invalidation voiding it, in event order."""
    claims = ClaimRow.objects.filter(source=source, source_id=source_id)
    voids = SourceInvalidationEvent.objects.filter(source=source, source_id=source_id)
    if source_version is not None:
        claims = claims.filter(source_version=source_version)
        voids = voids.filter(source_version__in=["", source_version])
    events: list[tuple[datetime, Claim | Invalidation]] = [
        (c.created_at, _claim(c)) for c in claims
    ]
    events += [(v.created_at, _invalidation(v)) for v in voids]
    return tuple(event for _, event in sorted(events, key=lambda pair: pair[0]))
