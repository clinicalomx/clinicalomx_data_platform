"""The store adapter: dataclasses, uid citations, decided_at idempotence. Subjects are ADP-*."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clinicalomx_ledger import adapter
from clinicalomx_ledger.models import Claim as ClaimRow
from clinicalomx_ledger.reads import Claim, Decision, Invalidation
from conftest import AGENT, HUMAN

DECIDED_AT = datetime(2026, 8, 24, tzinfo=UTC)


def test_append_claim_returns_a_dataclass_and_repeats_return_the_standing_row():
    written = adapter.append_claim(
        domain="marker", subject="ADP-CD8A", predicate="segmentation_role",
        value="marker", source="uniprot", source_id="ADP-P01732",
        source_version="2026_03", evidence="synthetic: surface",
        proposed_by="synthetic-agent", confidence=0.7, policy_version="ADP-policy-1",
        **AGENT, run_uid="run-adp-1",
    )
    assert isinstance(written, Claim)
    assert (written.confidence, written.policy_version, written.schema_version) == (
        0.7, "ADP-policy-1", 2)
    repeat = adapter.append_claim(
        domain="marker", subject="ADP-CD8A", predicate="segmentation_role",
        value="marker", source="uniprot", source_id="ADP-P01732",
        source_version="2026_03", evidence="synthetic: repeated read",
        proposed_by="synthetic-agent", **AGENT,
    )
    assert repeat.uid == written.uid
    assert [c.uid for c in adapter.claim_log("marker", "ADP-CD8A")] == [written.uid]


def test_append_decision_cites_its_claim_by_uid():
    cited = adapter.claim_log("marker", "ADP-CD8A")[0]
    decided = adapter.append_decision(
        domain="marker", claim_uid=cited.uid, outcome="accepted", by="synthetic-human",
        decided_at=DECIDED_AT, reason="synthetic signoff", **HUMAN,
    )
    assert isinstance(decided, Decision)
    assert (decided.claim_uid, decided.value, decided.outcome) == (
        cited.uid, "marker", "accepted")


def test_a_decision_on_no_claim_in_the_domain_is_refused():
    cited = adapter.claim_log("marker", "ADP-CD8A")[0]
    for domain, uid in (("marker", "clm-no-such-row"), ("reagent_mapping", cited.uid)):
        with pytest.raises(ClaimRow.DoesNotExist):
            adapter.append_decision(domain=domain, claim_uid=uid, outcome="accepted",
                                    by="synthetic-human", decided_at=DECIDED_AT, **HUMAN)


def test_the_same_decision_is_a_no_op_and_a_later_stated_date_is_not():
    cited = adapter.claim_log("marker", "ADP-CD8A")[0]
    base = dict(domain="marker", claim_uid=cited.uid, outcome="accepted",
                by="synthetic-human", reason="synthetic signoff", **HUMAN)
    assert adapter.append_decision(decided_at=DECIDED_AT, **base) is None
    reaffirmed = adapter.append_decision(decided_at=datetime(2026, 8, 25, tzinfo=UTC), **base)
    assert reaffirmed is not None
    assert len(adapter.decision_log("marker", "ADP-CD8A")) == 2


def test_the_logs_are_domain_scoped():
    adapter.append_invalidation(
        domain="reagent_mapping", source="human", source_id="ADP-shared-spelling",
        reason="synthetic: wrong sheet", occurred_at=DECIDED_AT,
        actor="synthetic-human", **HUMAN,
    )
    voided = adapter.invalidation_log("reagent_mapping")
    assert all(isinstance(v, Invalidation) for v in voided)
    assert any(v.source_id == "ADP-shared-spelling" for v in voided)
    assert not any(v.source_id == "ADP-shared-spelling"
                   for v in adapter.invalidation_log("marker"))
    assert adapter.decision_log("reagent_mapping", "ADP-CD8A") == ()


def test_an_invalidation_keeps_the_stated_date_apart_from_the_append_time():
    voided = [v for v in adapter.invalidation_log("reagent_mapping")
              if v.source_id == "ADP-shared-spelling"][0]
    assert voided.occurred_at == DECIDED_AT
    assert voided.created_at != voided.occurred_at
