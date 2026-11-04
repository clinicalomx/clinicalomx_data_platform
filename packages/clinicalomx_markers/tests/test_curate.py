"""The curator's write step and the review queue, against `MemoryClaimStore`.

Real captured UniProt responses and a temp-dir snapshot root. No mocks, no database.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.gate import Judgement, decisions_for, subject_state_for
from clinicalomx_ledger.grammar.model import short_id
from clinicalomx_ledger.grammar.store import MemoryClaimStore
from tests.conftest import ACCESSIONS, values_of

from clinicalomx_markers.curate.session import record_role, subject_row
from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    REVIEWED_PREDICATES,
    SUBJECT_TYPES,
    MarkerClaim,
    MarkerPredicate,
)
from clinicalomx_markers.propose.localisation import claims_from, record_from
from clinicalomx_markers.propose.role import RuleBasedRoleProposer, as_claim
from clinicalomx_markers.propose.schema import ProposalRequest
from clinicalomx_markers.query.claims import value_of
from clinicalomx_markers.sources.snapshot import capture
from clinicalomx_markers.sources.uniprot import url_for
from clinicalomx_markers.vocab import (
    TYPES,
    Outcome,
    ReviewState,
)


@pytest.fixture
def snapshot(snapshot_root, transport):
    """NAKATPASE as captured from UniProt, stored as the curator stores it."""
    accession = ACCESSIONS["NAKATPASE"]
    return capture(snapshot_root, source="uniprot", source_id=accession, retrieved_at="2026-07-31",
                   fetch=lambda: transport(url_for(accession)))


APPROVAL = Judgement(by="rafael", at="2026-08-24")


def store(**over) -> MemoryClaimStore:
    return MemoryClaimStore(MARKER_DOMAIN, **over)


def role_claim(**over) -> MarkerClaim:
    """A role claim."""
    base = {
        "subject": "NAKATPASE",
        "predicate": MarkerPredicate.SEGMENTATION_ROLE,
        "value": "cyto_structural",
        "source": "uniprot",
        "source_id": "P05023",
        "evidence": "Cell membrane; Multi-pass membrane protein.",
    }
    base.update(over)
    return MarkerClaim(**base)


def decide(claim, outcome, decisions=(), judgement=APPROVAL):
    """`decisions_for` with the marker domain bound."""
    return decisions_for(MARKER_DOMAIN, claim, outcome=outcome, decisions=list(decisions),
                         judgement=judgement)


# --- the ruling, which is the half that needs no instance --------------------------------

def test_an_outcome_is_always_a_subject_state():
    assert set(Outcome) <= set(ReviewState)
    assert set(ReviewState) - set(Outcome) == {ReviewState.UNRESOLVED}


def test_proposing_decides_nothing():
    assert decide(role_claim(), None, judgement=None) == []
    assert subject_state_for(MARKER_DOMAIN, [role_claim()], []) == ReviewState.UNRESOLVED


@pytest.mark.parametrize("outcome", Outcome)
def test_a_reviewed_claim_records_a_decision_with_that_outcome(outcome):
    (decided,) = decide(role_claim(), outcome)

    assert decided.outcome == outcome
    assert decided.value == "cyto_structural", "and the decision states what it ruled on"
    assert decided.claim_id == short_id(role_claim().claim_id())


@pytest.mark.parametrize("outcome", Outcome)
def test_the_subject_follows_the_newest_decision(outcome):
    first = decide(role_claim(), Outcome.ACCEPTED)
    second = decide(role_claim(), outcome, decisions=first,
                    judgement=Judgement(by="rafael", at="2026-08-25"))

    assert subject_state_for(MARKER_DOMAIN, [role_claim()], first + second) == outcome


def test_a_decision_needs_a_judgement():
    with pytest.raises(ValueError, match="needs a judgement"):
        decide(role_claim(), Outcome.ACCEPTED, judgement=None)


def test_a_re_affirmation_is_kept():
    """A second curator agreeing is a second decision. `Decision.decision` counts who and when."""
    accepted = decide(role_claim(), Outcome.ACCEPTED)
    (again,) = decide(role_claim(), Outcome.ACCEPTED, decisions=accepted,
                      judgement=Judgement(by="wenchao", at="2026-08-25"))

    assert again.outcome == Outcome.ACCEPTED
    assert again.by == "wenchao"


def test_an_identical_decision_is_dropped():
    accepted = decide(role_claim(), Outcome.ACCEPTED)

    assert decide(role_claim(), Outcome.ACCEPTED, decisions=accepted) == []


def test_a_subject_nothing_was_proposed_about_moves_nowhere():
    assert subject_state_for(MARKER_DOMAIN, [], []) is None


@pytest.mark.parametrize("predicate",
                         [MarkerPredicate.SUBCELLULAR_LOCATION, MarkerPredicate.GO_CC])
def test_evidence_claims_decide_nothing(predicate):
    """Localisation is read off the snapshot, so ruling on it is refused."""
    evidence = role_claim(predicate=predicate, value="Cell membrane")
    assert decide(evidence, None, judgement=None) == []
    assert subject_state_for(MARKER_DOMAIN, [evidence], []) is None
    with pytest.raises(ValueError, match="not reviewed"):
        decide(evidence, Outcome.ACCEPTED)


def test_the_reviewed_predicates_are_predicates():
    assert set(REVIEWED_PREDICATES) <= set(MarkerPredicate)
    assert set(REVIEWED_PREDICATES) <= set(values_of(MarkerPredicate))


def test_the_reviewed_predicates_are_the_domain_and_not_the_gate():
    """The marker domain reviews two predicates of six; the gate takes a `ClaimDomain`."""
    from clinicalomx_ledger.grammar import gate

    assert not hasattr(gate, "REVIEWED_PREDICATES")
    assert MARKER_DOMAIN.reviewed_predicates == REVIEWED_PREDICATES
    with pytest.raises(TypeError):
        decisions_for(role_claim(), outcome=None, decisions=[])


@pytest.mark.parametrize("subject_type", SUBJECT_TYPES)
def test_the_transition_reads_the_predicate_and_not_the_subject_type(subject_type):
    subject = "NAKATPASE-PCF"
    claims = store(subject_types={subject: subject_type})

    claims.add(role_claim(subject=subject))

    assert claims.in_state("unresolved") == [subject]
    assert claims.states[subject] == "unresolved"


# --- writing through the protocol -------------------------------------------------------

def test_the_load_step_writes_the_claim_through_the_store(snapshot):
    claims = store()
    record = record_from(snapshot)

    claim = record_role(claims, snapshot, record, marker="NAKATPASE",
                        role="cyto_structural", judgement=APPROVAL)

    assert claims.claims == [claim]
    assert claims.decisions("NAKATPASE")[-1].outcome == "accepted"
    assert "status" not in type(claim).model_fields


def test_the_written_claim_can_be_cited(snapshot):
    claims = store()
    record = record_from(snapshot)

    claim = record_role(claims, snapshot, record, marker="NAKATPASE",
                        role="cyto_structural", judgement=APPROVAL)

    assert claim.source == snapshot.source
    assert claim.source_id == snapshot.source_id
    assert claim.source_version == record.release
    assert claim.evidence == record.evidence


def test_an_accepted_claim_moves_the_subject_to_accepted(snapshot):
    claims = store()
    record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                role="cyto_structural", judgement=APPROVAL)

    assert claims.states["NAKATPASE"] == "accepted"
    assert claims.in_state("accepted") == ["NAKATPASE"]


def test_an_approval_carries_the_human_who_made_it(snapshot):
    claims = store()

    claim = record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                        role="cyto_structural", judgement=APPROVAL)

    decided = claims.decisions("NAKATPASE")[-1]
    assert decided.outcome == "accepted"
    assert (decided.by, decided.at) == (APPROVAL.by, APPROVAL.at)
    assert decided.kind == ""
    assert set(MarkerClaim.model_fields) & {"verdict_by", "verdict_at", "status"} == set(), \
        "and the claim does not restate any of it"
    assert claim.value == decided.value


def test_a_decision_naming_no_human_is_refused_before_anything_is_written(snapshot):
    claims = store()

    with pytest.raises(ValueError, match="by is blank"):
        Judgement(by="", at="2026-08-24")
    with pytest.raises(ValueError, match="at is blank"):
        Judgement(by="rafael", at="")
    with pytest.raises(ValueError, match="needs a judgement"):
        record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                    role="cyto_structural")

    assert claims.claims == []
    assert claims.decisions("NAKATPASE") == []
    assert claims.states == {}


def test_a_proposal_needs_no_judgement_and_writes_no_decision(snapshot):
    claims = store()

    claim = record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                        role="cyto_structural", outcome=None)

    assert claims.in_state(ReviewState.UNRESOLVED) == ["NAKATPASE"]
    assert claims.decisions("NAKATPASE") == [], "nobody has decided it"
    assert (claim.source, claim.source_id) == (snapshot.source, snapshot.source_id)


def test_a_handed_in_judgement_decides_nothing_on_a_proposal(snapshot):
    """The judgement is ignored, not refused."""
    claims = store()

    claim = record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                        role="cyto_structural", outcome=None, judgement=APPROVAL)

    assert claims.decisions("NAKATPASE") == []
    assert (claim.source, claim.source_id) == (snapshot.source, snapshot.source_id)
    assert claims.in_state(ReviewState.UNRESOLVED) == ["NAKATPASE"]


def test_a_rejection_is_written_with_its_decision(snapshot):
    claims = store()
    judgement = Judgement(kind="hint", reason="T-cell restricted, cannot outline every cell",
                          by="rafael", at="2026-08-12")

    record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                role="cyto_structural", outcome="rejected", judgement=judgement)

    decided = claims.decisions("NAKATPASE")[-1]
    assert decided.outcome == "rejected"
    assert (decided.kind, decided.reason) == ("hint", judgement.reason)
    assert (decided.by, decided.at) == ("rafael", "2026-08-12")
    assert claims.in_state("rejected") == ["NAKATPASE"]


def test_a_retry_leaves_the_earlier_attempt_in_place(snapshot):
    claims = store()
    record = record_from(snapshot)
    judgement = Judgement(kind="hint", reason="not settled by localisation", by="rafael",
                          at="2026-08-12")

    record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                outcome="rejected", judgement=judgement)
    record_role(claims, snapshot, record, marker="NAKATPASE", role="marker",
                judgement=APPROVAL)

    assert [c.value for c in claims.claims] == ["cyto_structural", "marker"]
    assert claims.in_state("accepted") == ["NAKATPASE"]
    assert claims.in_state("rejected") == []


def test_re_approving_after_a_rejection_lands_even_when_the_claim_is_identical(snapshot):
    """Approve, reject, re-approve from the same snapshot: one claim row and three decisions."""
    claims = store()
    record = record_from(snapshot)
    judgement = Judgement(kind="one_off", reason="wrong for this panel", by="rafael",
                          at="2026-08-19")

    approved = record_role(claims, snapshot, record, marker="NAKATPASE",
                           role="cyto_structural", judgement=APPROVAL)
    record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                outcome="rejected", judgement=judgement)
    again = record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                        judgement=APPROVAL)

    assert again == approved, "the same assertion, not a new one"
    assert claims.claims == [approved], "one assertion, one row"
    assert [str(d.outcome) for d in claims.decisions("NAKATPASE")] == [
        "accepted", "rejected", "accepted"]
    assert value_of(claims.answers("NAKATPASE"), "NAKATPASE",
                    MarkerPredicate.SEGMENTATION_ROLE).value == again.value
    assert claims.in_state("accepted") == ["NAKATPASE"], "the queue agrees with the answer"


def test_a_re_run_over_unchanged_evidence_appends_nothing(snapshot):
    """An unchanged re-run appends nothing. A superseded claim's re-assertion does land."""
    claims = store()
    record = record_from(snapshot)

    first = record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                        judgement=APPROVAL)
    again = record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                        judgement=APPROVAL)

    assert again == first
    assert claims.claims == [first]
    assert len(claims.decisions("NAKATPASE")) == 1, "one decision, not two"


def test_the_same_role_decided_again_is_a_second_decision(snapshot):
    """The same role approved again is one claim row and another decision."""
    claims = store()
    record = record_from(snapshot)
    later = Judgement(by=APPROVAL.by, at="2026-08-25")
    second_curator = Judgement(by="wenchao", at=APPROVAL.at)

    first = record_role(claims, snapshot, record, marker="NAKATPASE", role="cyto_structural",
                        judgement=APPROVAL)
    for judgement in (later, second_curator):
        again = record_role(claims, snapshot, record, marker="NAKATPASE",
                            role="cyto_structural", judgement=judgement)
        assert again == first, "the same assertion, decided again"

    assert claims.claims == [first], "one assertion, one row"

    decided = claims.decisions("NAKATPASE")
    assert [str(d.outcome) for d in decided] == ["accepted"] * 3
    assert [(d.by, d.at) for d in decided[1:]] == [(APPROVAL.by, "2026-08-25"),
                                                   ("wenchao", APPROVAL.at)]
    assert claims.in_state("accepted") == ["NAKATPASE"], "and the state still follows"


def test_the_load_step_enforces_the_rules(snapshot_root, transport):
    """`subject_row` is what the mitochondrial veto reads."""
    accession = ACCESSIONS["CITRATE_SYNTHASE"]
    snapshot = capture(snapshot_root, source="uniprot", source_id=accession,
                       retrieved_at="2026-07-31", fetch=lambda: transport(url_for(accession)))
    record = record_from(snapshot)
    assert "mitochondrion" in subject_row("CITRATE_SYNTHASE", record)["compartment"].lower()

    with pytest.raises(ValueError, match="forbidden"):
        record_role(store(), snapshot, record, marker="CITRATE_SYNTHASE",
                    role="cyto_structural", judgement=APPROVAL)


def test_a_refused_claim_never_reaches_the_store(snapshot):
    claims = store()

    with pytest.raises(ValueError, match="value"):
        record_role(claims, snapshot, record_from(snapshot), marker="NAKATPASE",
                    role="", judgement=APPROVAL)

    assert claims.claims == []
    assert claims.states == {}


# --- the queue --------------------------------------------------------------------------

def test_extracting_evidence_does_not_queue_the_marker(snapshot):
    claims = store()
    for claim in claims_from(snapshot, "NAKATPASE"):
        claims.add(claim)

    assert claims.claims, "the localisation claims should still have been written"
    assert claims.in_state("unresolved") == []
    assert claims.states == {}


def test_a_machine_proposal_reaches_the_queue_with_its_confidence(snapshot):
    """A machine proposal writes a claim with its confidence and no decision, and is queued."""
    record = record_from(snapshot)
    request = ProposalRequest(marker="NAKATPASE", evidence=record.evidence,
                              locations=record.locations, go_cc=record.go_cc)
    proposal = RuleBasedRoleProposer().propose(request)[0]
    claims = store()

    stored = claims.add(as_claim(proposal, request, source=snapshot.source,
                                 source_id=snapshot.source_id, evidence=record.evidence,
                                 source_version=record.release))

    assert stored.predicate == MarkerPredicate.SEGMENTATION_ROLE
    assert claims.in_state("unresolved") == ["NAKATPASE"]
    assert claims.decisions("NAKATPASE") == [], "nobody decided this yet"
    assert proposal.confidence > 0
    assert [c.confidence for c in claims.held("NAKATPASE")] == [proposal.confidence]


def test_the_queue_holds_what_is_waiting_for_a_human(snapshot):
    claims = store()
    claims.set_state("CD8A", "unresolved")
    claims.set_state("FOXP3", "unresolved")
    assert claims.in_state("unresolved") == ["CD8A", "FOXP3"]

    record_role(claims, snapshot, record_from(snapshot), marker="CD8A", role="marker",
                judgement=APPROVAL)

    assert claims.in_state("unresolved") == ["FOXP3"]
    assert claims.in_state("accepted") == ["CD8A"]


def test_an_unresolved_name_can_be_queued_without_a_claim():
    claims = store()
    claims.set_state("pNRFR2", "unresolved")
    assert claims.in_state("unresolved") == ["pNRFR2"]
    assert claims.claims == []


def test_an_unknown_state_is_refused():
    with pytest.raises(ValueError, match="not a valid ReviewState"):
        store().set_state("NAKATPASE", "pending")


def test_the_queue_reaches_a_channel_awaiting_a_role(snapshot):
    """Two channels of one marker wait in the queue independently."""
    channels = {"NAKATPASE-PCF": TYPES.IMAGING_CHANNEL,
                "NAKATPASE-CosMx": TYPES.IMAGING_CHANNEL}
    claims = store(subject_types=channels)
    for channel in channels:
        claims.add(role_claim(subject=channel))

    assert claims.in_state("unresolved") == ["NAKATPASE-CosMx", "NAKATPASE-PCF"]

    claims.add(role_claim(subject="NAKATPASE-PCF"), outcome="accepted", judgement=APPROVAL)

    assert claims.in_state("unresolved") == ["NAKATPASE-CosMx"]
    assert claims.in_state("accepted") == ["NAKATPASE-PCF"]
