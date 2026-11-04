"""The claim backends: the protocol, and the rules through the memory backend. Offline.

`LedgerClaimStore` is checked for protocol conformance only; its database behaviour runs in
`tests_pg/`. Do not mock lamindb or the ledger here.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import (
    ClaimStore,
    LedgerClaimStore,
    MemoryClaimStore,
)

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerClaim
from clinicalomx_markers.vocab import (
    TYPES,
    Outcome,
)


def make(**over) -> MarkerClaim:
    base = {
        "subject": "NAKATPASE",
        "predicate": "subcellular_location",
        "value": "Cell membrane",
        "source": "uniprot",
        "source_id": "P05023",
        "source_version": "2024-03",
        "evidence": "Cell membrane; Multi-pass membrane protein.",
    }
    base.update(over)
    return MarkerClaim(**base)


def store(**over) -> MemoryClaimStore:
    return MemoryClaimStore(MARKER_DOMAIN, **over)


@pytest.mark.parametrize("backend", [MemoryClaimStore, LedgerClaimStore])
def test_every_backend_satisfies_the_protocol(backend):
    """Constructing LedgerClaimStore touches neither the instance nor the ledger."""
    built = (LedgerClaimStore("dev", MARKER_DOMAIN, "tester", "human")
             if backend is LedgerClaimStore else MemoryClaimStore(MARKER_DOMAIN))
    assert isinstance(built, ClaimStore)


@pytest.mark.parametrize("backend", [MemoryClaimStore, LedgerClaimStore])
def test_no_backend_supplies_a_domain_of_its_own(backend):
    """No backend has a default domain, actor or actor_kind."""
    with pytest.raises(TypeError):
        backend("dev") if backend is LedgerClaimStore else backend()


def test_the_ledger_backend_does_not_default_its_actor():
    with pytest.raises(TypeError):
        LedgerClaimStore("dev", MARKER_DOMAIN)


# --- what a claim may be about, through the backends ------------------------------------

#: The judgement a human hands the store for a decision.
APPROVAL = Judgement(by="rafael", at="2026-08-24")


def role(**over) -> MarkerClaim:
    return make(**{"predicate": "segmentation_role", "value": "cyto_structural", **over})


def test_a_claim_about_a_channel_is_accepted_and_stored():
    s = store(subject_types={"NAKATPASE-PCF": TYPES.IMAGING_CHANNEL})

    stored = s.add(role(subject="NAKATPASE-PCF"), outcome="accepted", judgement=APPROVAL)

    assert s.claims == [stored]
    assert s.held("NAKATPASE-PCF") == [stored]
    assert s.states["NAKATPASE-PCF"] == "accepted"


def test_a_channel_subject_moves_through_the_queue_like_a_marker():
    s = store(subject_types={"NAKATPASE-PCF": TYPES.IMAGING_CHANNEL})

    s.add(role(subject="NAKATPASE-PCF"))

    assert s.in_state("unresolved") == ["NAKATPASE-PCF"]


def test_a_subject_that_is_neither_type_is_refused_by_the_memory_backend():
    s = store(subject_types={"DAPI": "Fluorophore"})

    with pytest.raises(ValueError, match="is a Fluorophore"):
        s.add(role(subject="DAPI"), outcome="accepted", judgement=APPROVAL)

    assert s.claims == []
    assert s.states == {}


def test_a_refused_subject_cannot_be_queued_either():
    """`set_state` also checks the subject."""
    s = store(subject_types={"DAPI": "Fluorophore"})

    with pytest.raises(ValueError, match="is a Fluorophore"):
        s.set_state("DAPI", "unresolved")

    assert s.states == {}


# --- append-only, through the backend ---------------------------------------------------

#: The judgement behind a rejection.
REFUSAL = Judgement(by=APPROVAL.by, at=APPROVAL.at, kind="one_off",
                    reason="wrong for this panel")

ACCEPT = (Outcome.ACCEPTED, APPROVAL)
REJECT = (Outcome.REJECTED, REFUSAL)


def walk(s: MemoryClaimStore, *steps, claim=None):
    """Put one claim through a sequence of decisions."""
    for outcome, judgement in steps:
        s.add(claim or role(), outcome=outcome, judgement=judgement)
    return s


def test_a_proposal_writes_no_decision_because_nobody_has_decided_it():
    s = store()
    s.add(role())

    assert s.decisions("NAKATPASE") == []
    assert s.rulings == []
    assert s.in_state("unresolved") == ["NAKATPASE"]


def test_a_decision_rules_on_the_claim_it_was_written_with():
    from clinicalomx_ledger.grammar.model import short_id

    s = store()
    written = s.add(role(), outcome="accepted", judgement=APPROVAL)

    assert s.decisions("NAKATPASE")[0].claim_id == short_id(written.claim_id())


def test_decide_rules_on_a_written_claim_without_asserting_it_again():
    """`decide` rejects a superseded reading without re-asserting it, as `add` would."""
    s = store()
    old = s.add(role(source_version="2026_02"), outcome="accepted", judgement=APPROVAL)
    s.add(role(value="marker", source_version="2026_09"))

    s.decide(old, outcome="rejected", judgement=REFUSAL)

    assert [c.value for c in s.held("NAKATPASE", predicate="segmentation_role")] == ["marker"]
    assert len(s.claims) == 2, "nothing was re-asserted"
    assert s.answers("NAKATPASE") == []


def test_decide_refuses_a_claim_nobody_wrote():
    s = store()
    with pytest.raises(ValueError, match="nothing to rule on"):
        s.decide(role(), outcome="accepted", judgement=APPROVAL)
    assert s.rulings == []


def test_an_unattributed_decision_cannot_be_written_through_the_store():
    """A reviewed claim with an outcome and no judgement writes nothing."""
    s = store()

    with pytest.raises(ValueError, match="needs a judgement"):
        s.add(role(), outcome="accepted")

    assert s.claims == []
    assert s.rulings == []
    assert s.states == {}


def test_a_judgement_on_an_unreviewed_predicate_is_ignored_on_a_proposal():
    s = store()
    s.add(make(), judgement=Judgement(by="rafael", at="2026-08-19"))

    assert s.rulings == []
    assert s.states == {}


def test_an_outcome_on_an_unreviewed_predicate_is_refused():
    """An outcome on an unreviewed predicate is refused, not dropped."""
    s = store()

    with pytest.raises(ValueError, match="not reviewed"):
        s.add(make(), outcome="accepted", judgement=APPROVAL)

    assert s.claims == []
    assert s.decisions("NAKATPASE") == []


def test_the_same_rejection_on_a_later_date_is_a_second_decision():
    """One assertion, three decisions."""
    s = walk(store(), REJECT,
             (Outcome.REJECTED, REFUSAL.model_copy(update={"at": "2026-09-01"})),
             (Outcome.REJECTED, REFUSAL.model_copy(update={"by": "wenchao"})))

    assert len(s.claims) == 1, "one assertion, decided three times"
    decided = s.decisions("NAKATPASE")
    assert [str(d.outcome) for d in decided] == ["rejected"] * 3
    assert [d.at for d in decided] == ["2026-08-24", "2026-09-01", "2026-08-24"]
    assert [d.by for d in decided] == ["rafael", "rafael", "wenchao"]


def test_an_identical_repeat_by_the_same_decider_is_a_no_op():
    s = walk(store(), REJECT, REJECT, REJECT)

    assert len(s.claims) == 1
    assert len(s.decisions("NAKATPASE")) == 1


def test_a_decision_is_one_record_carrying_its_own_reason():
    s = walk(store(), ACCEPT, REJECT)

    decided = s.decisions("NAKATPASE")[-1]
    assert decided.outcome == "rejected"
    assert decided.reason == "wrong for this panel"
    assert decided.by == "rafael"
    assert decided.value == "cyto_structural", "and what was decided, without a second read"


def test_every_ruling_is_its_own_row():
    """A correction is a later decision, never an edit."""
    s = walk(store(), ACCEPT, REJECT)
    s.add(role(source_version="2026_09"), outcome="accepted", judgement=APPROVAL)

    assert [str(d.outcome) for d in s.decisions("NAKATPASE")] == [
        "accepted", "rejected", "accepted"]
    assert [d.value for d in s.answers("NAKATPASE")] == ["cyto_structural"]


def test_a_retried_decision_lands_no_second_row():
    s = walk(store(), ACCEPT, ACCEPT)

    assert len(s.claims) == 1
    assert len(s.decisions("NAKATPASE")) == 1


def test_a_cycle_writes_every_lap_and_the_newest_decides():
    """Accept, reject, re-accept, reject again: each is written and the newest decides."""
    s = walk(store(), ACCEPT, REJECT, ACCEPT, REJECT)

    assert [str(d.outcome) for d in s.decisions("NAKATPASE")] == [
        "accepted", "rejected", "accepted", "rejected"]
    assert s.answers("NAKATPASE") == []
    assert s.states["NAKATPASE"] == "rejected", "the queue agrees with the newest decision"


def test_an_extracted_claim_decides_nothing():
    s = store()
    s.add(make())

    assert s.claims
    assert s.decisions("NAKATPASE") == []


def test_decisions_narrow_by_predicate_as_held_does():
    s = walk(store(), ACCEPT)
    s.add(make())

    assert s.decisions("NAKATPASE", predicate="segmentation_role")
    assert s.decisions("NAKATPASE", predicate="subcellular_location") == []


def test_decisions_hold_only_this_subject():
    s = store(subject_types={"NAKATPASE-PCF": TYPES.IMAGING_CHANNEL})
    s.add(role(), outcome="accepted", judgement=APPROVAL)
    s.add(role(subject="NAKATPASE-PCF"), outcome="rejected", judgement=REFUSAL)

    assert [d.outcome for d in s.decisions("NAKATPASE")] == ["accepted"]
    assert [d.outcome for d in s.decisions("NAKATPASE-PCF")] == ["rejected"]


def test_the_state_a_subject_lands_in_is_the_newest_decision():
    s = walk(store(), ACCEPT, REJECT)

    assert s.states["NAKATPASE"] == "rejected"
    assert s.states["NAKATPASE"] == s.decisions("NAKATPASE")[-1].outcome


def test_two_aliases_for_one_subject_are_both_the_answer():
    """Two accepted aliases both stand; rejecting one leaves the other."""
    s = store()
    cyto = s.add(role(source="pubmed", source_id="PMID:1"), outcome="accepted",
                 judgement=APPROVAL)
    s.add(role(value="marker", source="pubmed", source_id="PMID:2"), outcome="accepted",
          judgement=APPROVAL)
    assert sorted(d.value for d in s.answers("NAKATPASE")) == ["cyto_structural", "marker"]

    s.decide(cyto, outcome="rejected", judgement=REFUSAL)
    assert [d.value for d in s.answers("NAKATPASE")] == ["marker"]
