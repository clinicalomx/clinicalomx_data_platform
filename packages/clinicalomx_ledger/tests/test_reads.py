"""The five reads in `reads.py`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clinicalomx_ledger import reads, services
from conftest import AGENT, HUMAN


@pytest.fixture(scope="module", autouse=True)
def seeded():
    claims = [
        services.create_claim(
            domain="marker", subject="REF-FOXP3", predicate="segmentation_role",
            value="marker", source=source, source_id=source_id, source_version=version,
            evidence="synthetic: reads fixture", proposed_by="synthetic-agent",
            **AGENT, run_uid="run-ref-1",
        )
        for source, source_id, version in (("uniprot", "REF-Q9BZS1", "2026_03"),
                                           ("pubmed", "REF-PMID:9", ""))
    ]
    for claim in claims:
        services.decide(claim=claim, outcome="accepted", by="synthetic-human",
                        decided_at=datetime.now(UTC), reason="synthetic", **HUMAN,
                        run_uid="run-ref-2")
    services.invalidate_source(
        domain="marker", source="uniprot", source_id="REF-Q9BZS1",
        reason="synthetic: void for the by_source lookup", actor="synthetic-human", **HUMAN,
    )
    yield


def test_resolved_state_is_every_accepted_unvoided_claim():
    answer = reads.resolved_state("marker", "REF-FOXP3", "segmentation_role")
    assert [(a.value, a.decided_by) for a in answer] == [("marker", "synthetic-human")]
    assert reads.resolved_state("marker", "REF-FOXP3", "no_such_predicate") == ()


def test_decision_trail():
    trail = reads.decision_trail("marker", "REF-FOXP3")
    assert [(d.outcome, d.by, d.actor_kind) for d in trail] == [
        ("accepted", "synthetic-human", "human")] * 2
    assert len({d.claim_uid for d in trail}) == 2


def test_standing_claims_apply_voids():
    held = reads.standing_claims("marker", "REF-FOXP3", "segmentation_role")
    assert {c.source for c in held} == {"pubmed"}


def test_standing_claims_keep_every_value_of_a_multi_valued_predicate():
    for value in ("CDHE", "UVO"):
        services.create_claim(
            domain="marker", subject="REF-E-CADHERIN", predicate="alias", value=value,
            source="uniprot", source_id="REF-P12830", evidence="synthetic: a synonym",
            proposed_by="synthetic-agent", **AGENT,
        )
    held = reads.standing_claims("marker", "REF-E-CADHERIN", "alias", multi_valued=("alias",))
    assert sorted(c.value for c in held) == ["CDHE", "UVO"]
    (newest,) = reads.standing_claims("marker", "REF-E-CADHERIN", "alias")
    assert newest.value == "UVO"


def test_by_run():
    proposed = reads.by_run("run-ref-1")
    assert len(proposed) == 2
    assert all(event.run_uid == "run-ref-1" for event in proposed)
    decided = reads.by_run("run-ref-2")
    assert [d.outcome for d in decided] == ["accepted", "accepted"]


def test_by_source():
    events = reads.by_source("uniprot", "REF-Q9BZS1")
    kinds = [type(event).__name__ for event in events]
    assert kinds == ["Claim", "Invalidation"]


def test_a_row_from_before_the_three_tables_reads_the_same_way():
    """``schema_version`` 1 rows have no confidence and no policy."""
    from clinicalomx_ledger.models import Claim

    Claim.objects.create(
        uid="clm-ref-v1", schema_version=1, domain="marker", subject="REF-v1",
        predicate="segmentation_role", value="marker", source="url", source_id="REF-v1",
        evidence="synthetic: a migrated row", proposed_by="synthetic-agent", **AGENT,
    )
    (old,) = reads.standing_claims("marker", "REF-v1", "segmentation_role")
    assert (old.schema_version, old.confidence, old.policy_version) == (1, None, "")
