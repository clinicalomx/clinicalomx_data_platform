"""Domain-scoped logs and writes for a `ClaimStore` implementation, such as `LedgerClaimStore`.

Everything crosses as the frozen dataclasses `reads` defines, plus `Release`. A decision cites
its claim by uid; a uid matching no claim in the domain raises.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from . import services
from .models import Claim as ClaimRow
from .models import Decision as DecisionRow
from .models import Release as ReleaseRow
from .models import ReleaseEntry, SourceInvalidationEvent
from .reads import Claim, Decision, Invalidation, _claim, _decision, _invalidation


@dataclass(frozen=True)
class Release:
    """One release: who froze which approved set, and how many decisions it pins."""

    uid: str
    domain: str
    manifest_hash: str
    policy_version: str
    by: str
    actor_kind: str
    agent_model: str
    agent_model_version: str
    run_uid: str | None
    entries: int
    created_at: datetime


def _release(row: ReleaseRow) -> Release:
    return Release(
        uid=row.uid, domain=row.domain, manifest_hash=row.manifest_hash,
        policy_version=row.policy_version, by=row.by, actor_kind=row.actor_kind,
        agent_model=row.agent_model, agent_model_version=row.agent_model_version,
        run_uid=row.run_uid, entries=ReleaseEntry.objects.filter(release=row).count(),
        created_at=row.created_at,
    )


def claim(uid: str) -> Claim | None:
    """The claim row ``uid`` names, in whichever domain, or None when there is none."""
    row = ClaimRow.objects.filter(uid=uid).first()
    return None if row is None else _claim(row)


def queue(domain: str, *, multi_valued: Collection[str] = ()) -> tuple[Claim, ...]:
    """`services.queue` as rows."""
    return tuple(_claim(c) for c in services.queue(domain, multi_valued=multi_valued))


def answers(domain: str, subject: str | None = None) -> tuple[Decision, ...]:
    """`services.answers` as rows."""
    return tuple(_decision(d) for d in services.answers(domain, subject))


def releases(domain: str | None = None) -> tuple[Release, ...]:
    """Every release, oldest first, narrowed to ``domain`` when one is given."""
    rows = ReleaseRow.objects.all()
    if domain is not None:
        rows = rows.filter(domain=domain)
    return tuple(_release(r) for r in rows.order_by(*services.CREATION_ORDER))


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
    return _release(services.cut_release(
        domain=domain, by=by, actor_kind=actor_kind, agent_model=agent_model,
        agent_model_version=agent_model_version, policy_version=policy_version,
        run_uid=run_uid,
    ))


def claim_log(domain: str, subject: str,
              predicate: str | None = None) -> tuple[Claim, ...]:
    """Every claim for a subject, oldest first. The log, not a projection."""
    rows = ClaimRow.objects.filter(domain=domain, subject=subject)
    if predicate is not None:
        rows = rows.filter(predicate=predicate)
    return tuple(_claim(c) for c in rows.order_by(*services.CREATION_ORDER))


def decision_log(domain: str, subject: str,
                 predicate: str | None = None) -> tuple[Decision, ...]:
    """Every decision on a subject's claims, oldest first, domain-scoped."""
    rows = DecisionRow.objects.select_related("claim").filter(
        claim__domain=domain, claim__subject=subject)
    if predicate is not None:
        rows = rows.filter(claim__predicate=predicate)
    return tuple(_decision(d) for d in rows.order_by(*services.CREATION_ORDER))


def invalidation_log(domain: str) -> tuple[Invalidation, ...]:
    """Every void in a domain, oldest first."""
    rows = SourceInvalidationEvent.objects.filter(domain=domain)
    return tuple(_invalidation(v) for v in rows.order_by(*services.CREATION_ORDER))


def append_claim(
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
    return _claim(services.create_claim(
        domain=domain, subject=subject, predicate=predicate, value=value,
        source=source, source_id=source_id, source_version=source_version,
        evidence=evidence, proposed_by=proposed_by, actor_kind=actor_kind,
        agent_model=agent_model, agent_model_version=agent_model_version,
        confidence=confidence, policy_version=policy_version, run_uid=run_uid,
        multi_valued=multi_valued,
    ))


def append_decision(
    *,
    domain: str,
    claim_uid: str,
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
    decided = services.decide(
        claim=ClaimRow.objects.get(domain=domain, uid=claim_uid), outcome=outcome, by=by,
        actor_kind=actor_kind, decided_at=decided_at, kind=kind, reason=reason,
        agent_model=agent_model, agent_model_version=agent_model_version,
        policy_version=policy_version, run_uid=run_uid,
    )
    return None if decided is None else _decision(decided)


def append_invalidation(
    *,
    domain: str,
    source: str,
    source_id: str,
    source_version: str = "",
    reason: str,
    occurred_at: datetime,
    actor: str,
    actor_kind: str,
    agent_model: str = "",
    agent_model_version: str = "",
) -> Invalidation:
    return _invalidation(services.invalidate_source(
        domain=domain, source=source, source_id=source_id,
        source_version=source_version, reason=reason, actor=actor,
        actor_kind=actor_kind, agent_model=agent_model,
        agent_model_version=agent_model_version, occurred_at=occurred_at,
    ))
