"""The worked example from `docs/curation-recipe.md`, end to end on both backends.

Tests share state and run in file order: propose, accept, re-accept, supersede, reject, void.
"""

from __future__ import annotations

import lamindb as ln
import pytest

from clinicalomx_ledger import adapter
from clinicalomx_ledger.grammar.example import (
    EXAMPLE_DOMAIN,
    SampleClaim,
    SamplePredicate,
    SampleSource,
    SampleSourceInvalidation,
)
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import ClaimDomain
from clinicalomx_ledger.grammar.store import LedgerClaimStore, MemoryClaimStore

BRANCH = "recipe"
SUBJECT = "RCP-S-001"
#: A second subject, so the same assertion by each path is a peer, not a supersession.
BY_AGENT = "RCP-S-002"
BY_HUMAN = "RCP-S-003"

AGENT = {"actor": "recipe-session", "actor_kind": "agent",
         "agent_model": "synthetic-model", "agent_model_version": "1",
         "policy_version": "recipe-policy-1"}

HUMAN = {"actor": "synthetic-curator", "actor_kind": "human"}

APPROVAL = Judgement(by="synthetic-curator", at="2026-08-31", reason="block intact")
REJECTION = Judgement(by="synthetic-curator", at="2026-09-01", kind="one_off",
                      reason="consent withdrawn")


def disposition(**over) -> SampleClaim:
    base = {"subject": SUBJECT, "predicate": SamplePredicate.DISPOSITION,
            "value": "retain", "source": SampleSource.SOP, "source_id": "RCP-SOP-14",
            "source_version": "rev-3", "evidence": "Consent on file, block intact.",
            "confidence": 0.7}
    return SampleClaim(**{**base, **over})


@pytest.fixture(scope="module")
def stores():
    """Register the domain's subject types, subjects and branch, then one store per backend."""
    branch = ln.Branch(name=BRANCH).save()
    sample_type = ln.Record(name=EXAMPLE_DOMAIN.subject_types[0], is_type=True).save()
    for name in (SUBJECT, BY_AGENT, BY_HUMAN):
        ln.Record(name=name, type=sample_type, branch=branch).save()
    return (MemoryClaimStore(EXAMPLE_DOMAIN, subject_types={SUBJECT: "Sample"}),
            LedgerClaimStore(BRANCH, EXAMPLE_DOMAIN, **AGENT))


def test_the_declared_domain_needs_nothing_the_recipe_does_not_list():
    defaulted = {"policy", "multi_valued_predicates", "agent_may_accept"}
    stated = EXAMPLE_DOMAIN.model_fields_set
    assert not stated & defaulted, "the example should show the defaults, not restate them"
    assert stated == set(ClaimDomain.model_fields) - defaulted


def test_the_example_domain_walks_the_gate_on_both_backends(stores):
    memory, ledger = stores
    read = disposition()
    steps = [
        (read, {}),
        (read, {"outcome": "accepted", "judgement": APPROVAL}),
        (read, {"outcome": "accepted", "judgement": APPROVAL}),
        (disposition(value="discard", evidence="Consent withdrawn."), {}),
    ]
    for claim, kwargs in steps:
        memory.add(claim, **kwargs)
        ledger.add(claim, **kwargs)

    for history in (False, True):
        assert ledger.held(SUBJECT, history=history) == memory.held(SUBJECT, history=history)
    assert ledger.decisions(SUBJECT) == memory.decisions(SUBJECT)
    assert [c.value for c in ledger.held(SUBJECT)] == ["discard"]
    assert [str(d.outcome) for d in ledger.decisions(SUBJECT)] == ["accepted"], (
        "the proposal wrote no decision and the repeat decided nothing new"
    )
    assert [d.value for d in ledger.answers(SUBJECT)] == ["retain"], (
        "a later reading is not a ruling: the accepted answer stands until somebody rules"
    )
    assert ledger.in_state("unresolved") == [SUBJECT], "the new reading waits for review"

    for store in stores:
        store.decide(read, outcome="rejected", judgement=REJECTION)
    assert ledger.answers(SUBJECT) == memory.answers(SUBJECT) == []
    assert ledger.held(SUBJECT) == memory.held(SUBJECT), (
        "ruling on the superseded reading did not make the source say it again"
    )


def test_a_decision_is_appended_as_the_deciding_human(stores):
    """A human judgement is typed human; the claims keep the proposing agent and its policy."""
    decided = adapter.decision_log(EXAMPLE_DOMAIN.name, SUBJECT)
    assert {(d.actor_kind, d.by, d.policy_version) for d in decided} == {
        ("human", "synthetic-curator", None)
    }
    proposed = adapter.claim_log(EXAMPLE_DOMAIN.name, SUBJECT)
    assert {(c.actor_kind, c.agent_model, c.policy_version, c.confidence)
            for c in proposed} == {("agent", "synthetic-model", "recipe-policy-1", 0.7)}


def test_the_human_entry_point_is_a_peer_of_the_agentic_one():
    """Uses its own ``source_id`` so the void at the end of this file does not reach these rows."""
    said = {}
    for name, session in ((BY_AGENT, AGENT), (BY_HUMAN, HUMAN)):
        store = LedgerClaimStore(BRANCH, EXAMPLE_DOMAIN, **session)
        store.add(disposition(subject=name, source_id="RCP-SOP-15"))
        (said[name],) = adapter.claim_log(EXAMPLE_DOMAIN.name, name)

    typing = ("proposed_by", "actor_kind", "agent_model", "agent_model_version",
              "policy_version")
    identifying = ("uid", "subject", "created_at")
    shared = [f for f in type(said[BY_AGENT]).__dataclass_fields__
              if f not in typing + identifying]
    for field in shared:
        assert getattr(said[BY_AGENT], field) == getattr(said[BY_HUMAN], field), field
    assert (said[BY_HUMAN].actor_kind, said[BY_HUMAN].agent_model) == ("human", "")
    assert said[BY_AGENT].actor_kind == "agent"


def test_a_human_proposal_is_refused_uncitable_exactly_as_an_agent_one_is():
    for session in (AGENT, HUMAN):
        store = LedgerClaimStore(BRANCH, EXAMPLE_DOMAIN, **session)
        with pytest.raises(ValueError, match="not citable"):
            store.add(disposition(subject=BY_HUMAN, evidence="   "))


def test_a_void_is_a_humans_alone_and_deletes_nothing(stores):
    memory, ledger = stores
    void = SampleSourceInvalidation(
        source=SampleSource.SOP, source_id="RCP-SOP-14", source_version="rev-3",
        reason="rev-3 shifted the disposition column",
        by="synthetic-curator", at="2026-08-31",
    )
    memory.invalidate(void)
    ledger.invalidate(void)
    assert ledger.held(SUBJECT) == memory.held(SUBJECT) == []
    assert len(ledger.held(SUBJECT, history=True)) == 2, "nothing was deleted to make the view"
    assert [v.actor_kind for v in adapter.invalidation_log(EXAMPLE_DOMAIN.name)] == ["human"]
