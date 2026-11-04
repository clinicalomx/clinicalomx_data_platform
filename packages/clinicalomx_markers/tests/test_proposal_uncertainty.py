"""A proposal's uncertainty surface: confidence, ranked alternatives, and a triage label.

Every proposer emits it. The triage label is never an `Outcome` or `ReviewState`. Confidence
is stored on the claim.
"""
from __future__ import annotations

import dataclasses

import pytest
from clinicalomx_ledger.grammar.gate import ReviewState
from clinicalomx_ledger.grammar.model import Claim, Decision, Outcome
from clinicalomx_ledger.grammar.store import MemoryClaimStore
from pydantic import ValidationError
from tests.conftest import ACCESSIONS
from tests.test_llm_proposer import proposer as llm_proposer
from tests.test_llm_proposer import request_for as llm_request
from tests.test_propose import request_for

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerDecision
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.role import RuleBasedRoleProposer, as_claim
from clinicalomx_markers.propose.schema import (
    Attention,
    Candidate,
    Proposal,
    Triage,
    assess,
    review_order,
)
from clinicalomx_markers.vocab import MarkerPredicate, SegmentationRole

ROLE = MarkerPredicate.SEGMENTATION_ROLE

#: A full model answer: a choice, its confidence, and every other role ranked.
FULL_ANSWER = {
    "choice": "cyto_structural", "confidence": 0.9, "identity": "Na/K-ATPase",
    "alternatives": [{"choice": "marker", "confidence": 0.07},
                     {"choice": "nucleus", "confidence": 0.02},
                     {"choice": "exclude", "confidence": 0.01}],
    "reasoning": "membrane protein outlining the cell",
}


def proposal(value, confidence, *alternatives, label=None, reason="stated"):
    alts = tuple(Candidate(v, c) for v, c in alternatives)
    triage = Triage(label, reason) if label else assess(confidence, alts)
    return Proposal(predicate=ROLE, value=value, confidence=confidence, triage=triage,
                    alternatives=alts)


def assert_surface(got: Proposal) -> None:
    assert isinstance(got.confidence, float) and 0.0 <= got.confidence <= 1.0
    assert len(got.alternatives) >= 2, "one or two candidates leave a reviewer too little"
    confidences = [a.confidence for a in got.alternatives]
    assert confidences == sorted(confidences, reverse=True), "ranked best first"
    assert got.value not in {a.value for a in got.alternatives}
    assert isinstance(got.triage, Triage) and isinstance(got.triage.label, Attention)
    assert got.triage.reason.strip()


# --- every proposer emits the surface ------------------------------------------------------

@pytest.mark.parametrize("marker", sorted(ACCESSIONS))
def test_the_rule_based_proposer_emits_the_surface(marker, snapshot_root, transport):
    request, _ = request_for(marker, snapshot_root, transport)
    [got] = RuleBasedRoleProposer().propose(request)
    assert_surface(got)


def test_the_llm_proposer_emits_the_surface():
    [got] = llm_proposer([FULL_ANSWER]).propose(llm_request())
    assert_surface(got)
    assert got.confidence == 0.9
    assert [(a.value, a.confidence) for a in got.alternatives] == [
        ("marker", 0.07), ("nucleus", 0.02), ("exclude", 0.01)]
    assert got.triage.label is Attention.SKIM


def test_the_human_proposer_emits_the_surface(snapshot_root, transport):
    request, _ = request_for("NAKATPASE", snapshot_root, transport)
    person = HumanProposer(by="rafael")
    person.answer(request, SegmentationRole.CYTO_STRUCTURAL, confidence=0.7,
                  alternatives=(Candidate(SegmentationRole.NUCLEUS, 0.05),
                                Candidate(SegmentationRole.MARKER, 0.25)))
    [got] = person.propose(request)
    assert_surface(got)
    assert [a.value for a in got.alternatives] == ["marker", "nucleus"], \
        "the person's alternatives are ranked by the confidence they gave each"


def test_a_human_confidence_has_no_default(snapshot_root, transport):
    request, _ = request_for("NAKATPASE", snapshot_root, transport)
    with pytest.raises(TypeError, match="confidence"):
        HumanProposer(by="rafael").answer(request, SegmentationRole.CYTO_STRUCTURAL)


# --- the triage label is not a decision -----------------------------------------------------

def test_no_attention_label_is_spelled_like_a_status_or_a_state():
    labels = {a.value for a in Attention}
    assert not labels & {s.value for s in Outcome}
    assert not labels & {s.value for s in ReviewState}


@pytest.mark.parametrize("label", list(Attention))
def test_an_attention_label_cannot_be_read_as_a_status(label):
    assert not isinstance(label, str), "a str enum would pass anywhere a status string is read"
    for vocabulary in (Outcome, ReviewState):
        with pytest.raises(ValueError):
            vocabulary(label)
        with pytest.raises(ValueError):
            vocabulary(label.value)


@pytest.mark.parametrize("label", list(Attention))
def test_an_attention_label_cannot_be_stored_as_a_decision(label):
    with pytest.raises(ValidationError):
        MarkerDecision(subject="NAKATPASE", predicate=ROLE, value="cyto_structural",
                       claim_id="NAKATPASE:segmentation_role:uniprot:P05023:abc123def456",
                       outcome=label, by="rafael", at="2026-09-25")


@pytest.mark.parametrize("label", list(Attention))
def test_an_attention_label_cannot_be_stored_as_a_review_state(label):
    store = MemoryClaimStore(MARKER_DOMAIN)
    with pytest.raises(ValueError):
        store.set_state("NAKATPASE", label)
    assert "NAKATPASE" not in store.states


def test_an_outcome_cannot_be_a_triage_label():
    for status in Outcome:
        with pytest.raises(TypeError):
            Triage(status, "the lab decided")
    with pytest.raises(TypeError):
        Triage("skim", "a plain string is not a label")


def test_the_label_and_the_status_share_no_field():
    """A proposal has no outcome; claims and decisions have no label."""
    fields = {f.name: f.type for f in dataclasses.fields(Proposal)}
    assert "outcome" not in fields and not any("Outcome" in str(t) for t in fields.values())
    for model in (Claim, Decision):
        assert not {"triage", "attention"} & set(model.model_fields)


def test_a_claim_made_from_a_proposal_carries_no_label(snapshot_root, transport):
    request, snap = request_for("CITRATE_SYNTHASE", snapshot_root, transport)
    [got] = RuleBasedRoleProposer().propose(request)
    claim = as_claim(got, request, source="uniprot", source_id=snap.source_id,
                     evidence=request.evidence)
    assert got.triage.label.value not in {str(v) for v in claim.model_dump().values()}


@pytest.mark.parametrize("marker", sorted(ACCESSIONS))
def test_the_confidence_outlives_the_proposal(marker, snapshot_root, transport):
    """`as_claim` stores the winner's confidence on the claim."""
    request, snap = request_for(marker, snapshot_root, transport)
    [got] = RuleBasedRoleProposer().propose(request)
    store = MemoryClaimStore(MARKER_DOMAIN)
    store.add(as_claim(got, request, source="uniprot", source_id=snap.source_id,
                       evidence=request.evidence))
    assert [c.confidence for c in store.held(marker)] == [got.confidence]


def test_a_reviewer_can_sort_stored_claims_by_confidence(snapshot_root, transport):
    store = MemoryClaimStore(MARKER_DOMAIN)
    made = {}
    for marker in sorted(ACCESSIONS):
        request, snap = request_for(marker, snapshot_root, transport)
        [got] = RuleBasedRoleProposer().propose(request)
        made[marker] = got.confidence
        store.add(as_claim(got, request, source="uniprot", source_id=snap.source_id,
                           evidence=request.evidence))
    stored = [c for marker in made for c in store.held(marker, predicate=ROLE)]
    by_confidence = sorted(stored, key=lambda c: -c.confidence)
    assert [c.confidence for c in by_confidence] == sorted(made.values(), reverse=True)


def test_a_label_without_a_reason_is_refused():
    with pytest.raises(ValueError, match="no reason"):
        Triage(Attention.REVIEW, "  ")


# --- the shape refuses what it cannot carry -----------------------------------------------

@pytest.mark.parametrize("bad", [-0.1, 1.5, True, "0.9", None])
def test_a_confidence_outside_zero_to_one_is_refused(bad):
    with pytest.raises(ValueError, match="confidence"):
        proposal("marker", bad, label=Attention.REVIEW)
    with pytest.raises(ValueError, match="confidence"):
        Candidate("marker", bad)


def test_unranked_alternatives_are_refused():
    with pytest.raises(ValueError, match="ranked"):
        proposal("cyto_structural", 0.6, ("nucleus", 0.1), ("marker", 0.3))


def test_the_winner_is_not_also_an_alternative():
    with pytest.raises(ValueError, match="both"):
        proposal("marker", 0.6, ("marker", 0.3), ("nucleus", 0.1))


# --- where the default triage lands --------------------------------------------------------

def test_assess_skims_only_a_confident_clear_winner():
    assert assess(0.9, (Candidate("marker", 0.1),)).label is Attention.SKIM
    assert assess(0.9, (Candidate("marker", 0.7),)).label is Attention.REVIEW
    assert assess(0.6, (Candidate("marker", 0.1),)).label is Attention.REVIEW
    assert assess(0.3, (Candidate("marker", 0.1),)).label is Attention.SCRUTINISE


def test_a_structural_call_from_localisation_alone_is_never_skimmed(snapshot_root, transport):
    """A structural call from localisation alone is never skimmed."""
    request, _ = request_for("CD8A", snapshot_root, transport)
    [got] = RuleBasedRoleProposer().propose(request)
    assert got.value == "cyto_structural"
    assert got.triage.label is not Attention.SKIM


def test_a_rule_demotion_is_scrutinised_and_drops_the_blocked_value(snapshot_root, transport):
    request, _ = request_for("CITRATE_SYNTHASE", snapshot_root, transport,
                             locations=("Cell membrane", "Mitochondrion matrix"))
    [got] = RuleBasedRoleProposer().propose(request)
    assert got.value == "marker"
    assert got.triage.label is Attention.SCRUTINISE
    assert "cyto_structural" in got.triage.reason
    assert "cyto_structural" not in {a.value for a in got.alternatives}
    assert_surface(got)


def test_the_llm_demotion_is_scrutinised_and_drops_the_blocked_value():
    [got] = llm_proposer([FULL_ANSWER]).propose(
        llm_request("CITRATE_SYNTHASE", ("Mitochondrion matrix",)))
    assert got.value == "marker" and got.confidence == 0.07
    assert got.triage.label is Attention.SCRUTINISE
    assert [a.value for a in got.alternatives] == ["nucleus", "exclude"]


def test_a_model_that_states_no_confidence_is_scrutinised_not_guessed():
    [got] = llm_proposer([{"choice": "marker", "identity": "x",
                           "reasoning": "y"}]).propose(llm_request())
    assert got.confidence == 0.0
    assert got.triage.label is Attention.SCRUTINISE
    assert "no usable confidence" in got.triage.reason


def test_a_model_ranking_a_runner_up_above_its_choice_is_scrutinised():
    answer = {**FULL_ANSWER, "confidence": 0.4,
              "alternatives": [{"choice": "marker", "confidence": 0.5}]}
    [got] = llm_proposer([answer]).propose(llm_request())
    assert got.confidence == 0.4 and got.alternatives[0].confidence == 0.5, \
        "the model's numbers are kept as stated"
    assert got.triage.label is Attention.SCRUTINISE and got.margin < 0


def test_unusable_model_alternatives_are_dropped_not_repaired():
    answer = {**FULL_ANSWER, "alternatives": [
        {"choice": "cyto_structural", "confidence": 0.5},  # the choice itself
        {"choice": "nonsense", "confidence": 0.3},
        {"choice": "marker", "confidence": 3},
        {"choice": "nucleus", "confidence": 0.2},
        {"choice": "nucleus", "confidence": 0.1},
        "exclude",
    ]}
    [got] = llm_proposer([answer]).propose(llm_request())
    assert [(a.value, a.confidence) for a in got.alternatives] == [("nucleus", 0.2)]


def test_a_model_refusal_is_scrutinised():
    [got] = llm_proposer([{"choice": "REFUSE", "confidence": 0.9, "identity": "UNKNOWN",
                           "alternatives": [], "reasoning": "no evidence"}]).propose(
        llm_request("CD9999"))
    assert got.refused and got.confidence == 0.0
    assert got.triage.label is Attention.SCRUTINISE and "no evidence" in got.triage.reason


# --- the review queue ----------------------------------------------------------------------

def test_the_queue_puts_the_most_attention_first():
    queue = [
        ("SKIMMED", proposal("marker", 0.9, ("nucleus", 0.05), ("exclude", 0.0))),
        ("CLOSE", proposal("cyto_structural", 0.6, ("marker", 0.55), ("nucleus", 0.0))),
        ("CLEAR", proposal("cyto_structural", 0.6, ("marker", 0.1), ("nucleus", 0.0))),
        ("DEMOTED", proposal("marker", 0.7, ("nucleus", 0.1), ("exclude", 0.0),
                             label=Attention.SCRUTINISE, reason="a rule blocked it")),
        ("UNSURE", proposal("marker", 0.3, ("nucleus", 0.2), ("exclude", 0.0))),
        ("SURER", proposal("marker", 0.7, ("nucleus", 0.1), ("exclude", 0.0))),
    ]
    assert [subject for subject, _ in review_order(queue)] == [
        # scrutinise first, least sure first
        "UNSURE", "DEMOTED",
        # then review: least sure, then the closest race with its runner-up
        "CLOSE", "CLEAR", "SURER",
        # skim last
        "SKIMMED",
    ]


def test_the_queue_order_is_total_and_repeatable():
    same = proposal("marker", 0.6, ("nucleus", 0.1), ("exclude", 0.0))
    queue = [("B", same), ("A", same), ("C", same)]
    assert [s for s, _ in review_order(queue)] == ["A", "B", "C"]
    assert review_order(reversed(queue)) == review_order(queue)


def test_real_proposals_queue_the_doubtful_before_the_safe(snapshot_root, transport):
    """Over captured fixtures, the organelle veto is skimmed last."""
    queue = []
    for marker in sorted(ACCESSIONS):
        request, _ = request_for(marker, snapshot_root, transport)
        queue.append((marker, RuleBasedRoleProposer().propose(request)[0]))
    ordered = [s for s, _ in review_order(queue)]
    assert ordered[-1] == "CITRATE_SYNTHASE"
    assert ordered.index("CD8A") < ordered.index("CITRATE_SYNTHASE")
