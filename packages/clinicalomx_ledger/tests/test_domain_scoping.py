"""Two domains asserting the same (subject, predicate) stay independent. Subjects are DSC-*."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clinicalomx_ledger import reads, services
from conftest import AGENT, HUMAN

LEFT = "dsc_left"
RIGHT = "dsc_right"

SUBJECT = "DSC-shared-subject"
PREDICATE = "disposition"


def claim_in(domain: str, value: str, source_id: str):
    return services.create_claim(
        domain=domain, subject=SUBJECT, predicate=PREDICATE, value=value,
        source="url", source_id=source_id, evidence=f"synthetic: {domain} read",
        proposed_by="synthetic-agent", **AGENT,
    )


@pytest.fixture(scope="module", autouse=True)
def two_domains():
    """One claim and one decision per domain, on the same subject and predicate."""
    for domain, value, source_id in ((LEFT, "retain", "DSC-left-1"),
                                     (RIGHT, "discard", "DSC-right-1")):
        services.decide(claim=claim_in(domain, value, source_id), outcome="accepted",
                        by=f"synthetic-{domain}-curator", decided_at=datetime.now(UTC),
                        **HUMAN)


def test_each_domain_keeps_its_own_answer():
    left = reads.resolved_state(LEFT, SUBJECT, PREDICATE)
    right = reads.resolved_state(RIGHT, SUBJECT, PREDICATE)
    assert [(a.domain, a.value) for a in left] == [(LEFT, "retain")]
    assert [(a.domain, a.value) for a in right] == [(RIGHT, "discard")]


def test_a_domain_sees_only_its_own_standing_claims():
    for domain, expected in ((LEFT, "retain"), (RIGHT, "discard")):
        held = reads.standing_claims(domain, SUBJECT, PREDICATE)
        assert [(c.domain, c.value) for c in held] == [(domain, expected)]


def test_a_domain_sees_only_its_own_decisions():
    for domain in (LEFT, RIGHT):
        trail = reads.decision_trail(domain, SUBJECT, PREDICATE)
        assert [d.domain for d in trail] == [domain]


def test_a_domain_queues_only_its_own_claims():
    claim_in(LEFT, "undecided", "DSC-left-2")
    assert [c.source_id for c in services.queue(LEFT)] == ["DSC-left-2"]
    assert services.queue(RIGHT) == []


def test_one_domains_void_does_not_reach_the_others_claims():
    services.invalidate_source(
        domain=LEFT, source="url", source_id="DSC-left-1",
        reason="synthetic: the left domain voids its own source",
        actor="synthetic-human", **HUMAN,
    )
    assert reads.resolved_state(LEFT, SUBJECT, PREDICATE) == ()
    assert len(reads.resolved_state(RIGHT, SUBJECT, PREDICATE)) == 1
    assert [c.source_id for c in reads.standing_claims(LEFT, SUBJECT, PREDICATE)] == [
        "DSC-left-2"
    ]
    assert len(reads.standing_claims(RIGHT, SUBJECT, PREDICATE)) == 1
