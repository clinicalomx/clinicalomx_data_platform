"""A claim must be citable and permitted before it is written. No database needed."""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import (
    NAME_LIMIT,
    check_subject_type,
    short_id,
    stored_duplicate,
    validate,
)
from clinicalomx_ledger.grammar.store import (
    ClaimStore,
    MemoryClaimStore,
)

from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    SUBJECT_TYPES,
    MarkerClaim,
    MarkerDecision,
    MarkerPredicate,
    MarkerSource,
)
from clinicalomx_markers.query.claims import value_of
from clinicalomx_markers.vocab import TYPES

APPROVAL = Judgement(by="rafael", at="2026-08-24")


def make(**over) -> MarkerClaim:
    base = {
        "subject": "NAKATPASE",
        "predicate": "subcellular_location",
        "value": "Cell membrane",
        "source": "uniprot",
        "source_id": "P05023",
        "evidence": "Cell membrane; Multi-pass membrane protein.",
        "source_version": "2024-03",
    }
    base.update(over)
    return MarkerClaim(**base)


def role(**over) -> MarkerClaim:
    """A claim on the human-reviewed predicate."""
    return make(**{"predicate": MarkerPredicate.SEGMENTATION_ROLE,
                   "value": "cyto_structural", **over})


def store(**over) -> MemoryClaimStore:
    return MemoryClaimStore(MARKER_DOMAIN, **over)


def test_a_complete_claim_validates():
    validate(make())


@pytest.mark.parametrize("field", ["subject", "source_id", "evidence", "value"])
def test_missing_provenance_is_refused(field):
    with pytest.raises(ValueError, match=field):
        validate(make(**{field: ""}))


@pytest.mark.parametrize("blank", ["   ", "\t", "\n"])
def test_whitespace_is_not_a_value(blank):
    with pytest.raises(ValueError, match="evidence"):
        validate(make(evidence=blank))


@pytest.mark.parametrize("field", ["value", "source_id", "evidence"])
def test_a_refused_claim_never_reaches_the_store(field):
    s = store()
    with pytest.raises(ValueError, match=field):
        s.add(make(**{field: ""}))
    assert s.held("NAKATPASE") == []


def test_a_blank_source_is_refused_before_validate_ever_sees_it():
    with pytest.raises(ValueError, match="source"):
        make(source="")


def test_an_unknown_source_is_refused_at_construction():
    with pytest.raises(ValueError, match="source"):
        make(source="wikipedia")


def test_a_claim_cannot_be_given_a_status_at_all():
    assert "status" not in MarkerClaim.model_fields
    with pytest.raises(ValueError, match="status"):
        make(status="approved")


def test_a_retired_field_is_refused_rather_than_ignored():
    """pydantic ignores unknown keywords by default; `extra="forbid"` makes them an error."""
    with pytest.raises(ValueError, match="verdict_by"):
        make(verdict_by="rafael")


def test_a_member_is_the_string_it_spells():
    assert make(source="uniprot").source == "uniprot"
    assert make(source=MarkerSource.UNIPROT) == make(source="uniprot")
    assert str(make(source="uniprot").source) == "uniprot"


def test_claim_id_is_stable_and_distinguishes_rival_claims():
    a = make(value="Cell membrane")
    assert a.claim_id() == make(value="Cell membrane").claim_id()
    assert a.claim_id() != make(value="Mitochondrion").claim_id()
    assert a.claim_id() != make(source="pubmed", source_id="PMID:1").claim_id()


def test_the_same_value_at_two_releases_is_two_ids():
    older = make(source_version="2026_02")
    newer = make(source_version="2026_09")

    assert older != newer
    assert older.claim_id() != newer.claim_id()
    assert short_id(older.claim_id()) != short_id(newer.claim_id())
    assert older.claim_id() == make(source_version="2026_02").claim_id()


def test_a_long_value_still_yields_a_short_id():
    long_value = " ".join(f"GO:{n:07d} some cellular component name" for n in range(40))

    assert len(make(value=long_value).claim_id()) < 80


def test_two_long_values_that_differ_late_do_not_collide():
    shared = "GO:0005886 plasma membrane. " * 8
    first = make(value=f"{shared} GO:0005739 mitochondrion")
    second = make(value=f"{shared} GO:0005634 nucleus")

    assert first.claim_id() != second.claim_id()
    assert short_id(first.claim_id()) != short_id(second.claim_id())


def test_an_id_too_long_for_the_label_column_is_shortened_and_still_unique():
    stem = "https://example.org/" + "p" * NAME_LIMIT
    first = make(source="url", source_id=f"{stem}/one")
    second = make(source="url", source_id=f"{stem}/two")

    assert len(short_id(first.claim_id())) == NAME_LIMIT
    assert short_id(first.claim_id()) != short_id(second.claim_id())
    assert short_id("short:enough:already") == "short:enough:already"


def test_human_supplied_url_is_a_first_class_source():
    validate(make(source="url", source_id="https://example.org/paper", source_version=""))


def test_the_memory_backend_satisfies_the_store_protocol():
    assert isinstance(store(), ClaimStore)


# --- what a claim may be about ----------------------------------------------------------

@pytest.mark.parametrize("subject_type", SUBJECT_TYPES)
def test_both_subject_types_are_permitted(subject_type):
    check_subject_type("NAKATPASE", subject_type, permitted=SUBJECT_TYPES)


def test_the_channel_is_in_the_permitted_set():
    """`segmentation_role` is a fact about a channel."""
    assert TYPES.IMAGING_CHANNEL in SUBJECT_TYPES


def test_the_permitted_set_has_no_default():
    with pytest.raises(TypeError, match="permitted"):
        check_subject_type("NAKATPASE", "Marker")


def test_a_subject_that_is_neither_is_refused_and_says_what_it_found():
    with pytest.raises(ValueError, match="is a Fluorophore"):
        check_subject_type("DAPI", "Fluorophore", permitted=SUBJECT_TYPES)


def test_a_missing_record_still_says_so_and_names_both_types():
    with pytest.raises(ValueError) as raised:
        check_subject_type("pNRFR2", None, permitted=SUBJECT_TYPES, where="dev")

    message = str(raised.value)
    assert "no Marker or ImagingChannel record named 'pNRFR2'" in message
    assert "on branch 'dev'" in message
    assert "A claim subject is a Record link" in message


def test_the_branch_is_only_named_when_there_is_one():
    with pytest.raises(ValueError) as raised:
        check_subject_type("pNRFR2", None, permitted=SUBJECT_TYPES)

    assert "on branch" not in str(raised.value)


def test_re_approving_the_same_claim_writes_nothing_and_returns_what_is_stored():
    s = store()
    first = s.add(role(), outcome="accepted", judgement=APPROVAL)

    again = s.add(role(), outcome="accepted", judgement=APPROVAL)

    assert again == first
    assert s.held("NAKATPASE") == [first]
    assert len(s.claims) == 1


def test_proposing_then_approving_is_one_claim_and_two_decisions():
    s = store()
    s.add(role())
    assert s.decisions("NAKATPASE") == []
    s.add(role(), outcome="accepted", judgement=APPROVAL)

    assert len(s.claims) == 1
    assert [str(d.outcome) for d in s.decisions("NAKATPASE")] == ["accepted"]


def test_a_repeat_is_a_no_op():
    s = store()
    proposed = s.add(make())

    assert s.add(make()) == proposed
    assert s.claims == [proposed]

    rival = s.add(make(value="Basolateral only"))
    assert s.claims == [proposed, rival]


def test_a_repeat_leaves_the_subject_where_its_decisions_put_it():
    s = store()
    s.add(role(), outcome="accepted", judgement=APPROVAL)
    s.set_state("NAKATPASE", "unresolved")

    s.add(role(), outcome="accepted", judgement=APPROVAL)

    assert s.in_state("accepted") == ["NAKATPASE"]
    assert s.in_state("unresolved") == []


def test_one_marker_can_hold_an_approved_role_per_channel():
    channels = {"NAKATPASE-PCF": TYPES.IMAGING_CHANNEL,
                "NAKATPASE-CosMx": TYPES.IMAGING_CHANNEL}
    s = store(subject_types=channels)

    s.add(role(subject="NAKATPASE-PCF"), outcome="accepted", judgement=APPROVAL)
    s.add(role(subject="NAKATPASE-CosMx", value="marker"),
          outcome="accepted", judgement=APPROVAL)

    def role_of(subject):
        return value_of(s.answers(subject), subject, MarkerPredicate.SEGMENTATION_ROLE)

    assert role_of("NAKATPASE-PCF").value == "cyto_structural"
    assert role_of("NAKATPASE-CosMx").value == "marker"


def test_conflicting_sources_may_both_exist():
    s = store()
    s.add(make())
    s.add(make(source="pubmed", source_id="PMID:1", value="Basolateral only"))
    assert len(s.held("NAKATPASE")) == 2
    assert s.held("NAKATPASE", predicate="subcellular_location")[0].source == "uniprot"


# --- the newest decision is the answer -----------------------------------------------------

def answer(s: MemoryClaimStore):
    """What a consumer reading this store gets for NAKATPASE's segmentation role."""
    return value_of(s.answers("NAKATPASE"), "NAKATPASE", MarkerPredicate.SEGMENTATION_ROLE)


def test_a_rejection_retracts_the_approval_it_follows():
    s = store()
    s.add(role(), outcome="accepted", judgement=APPROVAL)
    s.add(role(), outcome="rejected",
          judgement=Judgement(by="rafael", at="2026-08-25", kind="one_off", reason="no"))

    assert answer(s) is None
    assert len(s.decisions("NAKATPASE")) == 2, "both rows stay readable, this is not a delete"

    s.add(role(value="marker"), outcome="accepted",
          judgement=Judgement(by="rafael", at="2026-08-26"))
    assert answer(s).value == "marker", "a correction: a new claim, accepted, is the answer"


def test_a_re_assertion_at_a_new_source_version_is_accepted():
    s = store()
    s.add(role(source_version="2026_02"), outcome="accepted", judgement=APPROVAL)
    s.add(role(source_version="2026_09"), outcome="accepted",
          judgement=Judgement(by="rafael", at="2026-08-25"))

    assert answer(s).value == "cyto_structural"
    assert [c.source_version for c in s.claims] == ["2026_02", "2026_09"]


def test_a_restated_rejection_leaves_the_same_answer():
    s = store()
    refusal = Judgement(by="rafael", at="2026-08-25", kind="one_off", reason="no")
    s.add(role(), outcome="accepted", judgement=APPROVAL)
    s.add(role(), outcome="rejected", judgement=refusal)
    s.add(role(), outcome="rejected", judgement=refusal)

    assert answer(s) is None
    assert len(s.claims) == 1
    assert [str(d.outcome) for d in s.decisions("NAKATPASE")] == ["accepted", "rejected"]


def test_an_unreviewed_predicate_cannot_be_decided_at_all():
    s = store()
    with pytest.raises(ValueError, match="not reviewed"):
        s.add(make(), outcome="accepted", judgement=APPROVAL)
    assert s.claims == [], "a refused decision leaves no claim row behind either"

    s.add(make())
    assert s.decisions("NAKATPASE") == [], "proposing it is still the ordinary extract path"


def test_both_backends_answer_from_the_same_ordering():
    """Both backends return `answers` oldest first; the shared read layer takes the last.

    The ledger half runs in `tests_pg/`.
    """
    s = store()
    s.add(role(), outcome="accepted", judgement=APPROVAL)
    s.add(role(value="marker"), outcome="accepted",
          judgement=Judgement(by="rafael", at="2026-08-25"))

    assert [d.value for d in s.answers("NAKATPASE")] == ["cyto_structural", "marker"]
    assert not set(MarkerClaim.model_fields) & {"created_at", "id"}
    assert not set(MarkerDecision.model_fields) & {"created_at", "id"}
    assert answer(s).value == "marker"


def test_a_repeat_is_still_recognised_by_the_shared_rule():
    stored = make()

    assert stored_duplicate(stored, [stored]) == stored
    assert stored_duplicate(stored, []) is None
    assert stored_duplicate(make(value="Mitochondrion"), [stored]) is None


def test_a_rule_blocks_a_forbidden_assertion():
    """The rule arrives as `MARKER_DOMAIN.policy`, not a hardcoded branch."""
    with pytest.raises(ValueError, match="forbidden"):
        validate(
            make(subject="CITRATE_SYNTHASE", predicate="segmentation_role",
                 value="cyto_structural"),
            subject_row={"canonical_name": "CITRATE_SYNTHASE",
                         "compartment": "Mitochondrion matrix"},
            policy=MARKER_DOMAIN.policy,
        )


def test_the_same_assertion_is_fine_where_the_rule_does_not_apply():
    validate(
        make(subject="VIMENTIN", predicate="segmentation_role", value="cyto_structural"),
        subject_row={"canonical_name": "VIMENTIN", "compartment": "cytoplasm|cytoskeleton"},
        policy=MARKER_DOMAIN.policy,
    )


def test_the_policy_check_stays_beside_the_model_and_not_in_it():
    forbidden = make(subject="CITRATE_SYNTHASE", predicate="segmentation_role",
                     value="cyto_structural")
    assert forbidden.value == "cyto_structural", "constructing it is not the check"
    with pytest.raises(ValueError, match="forbidden"):
        validate(forbidden, subject_row={"canonical_name": "CITRATE_SYNTHASE",
                                         "compartment": "Mitochondrion matrix"},
                 policy=MARKER_DOMAIN.policy)


# --- the decision, which rules on one claim and is not a state -------------------------------

CLAIM_ID = "NAKATPASE:segmentation_role:uniprot:P05023:abc123def456"


def decision(**over) -> MarkerDecision:
    base = {"subject": "NAKATPASE", "predicate": MarkerPredicate.SEGMENTATION_ROLE,
            "value": "cyto_structural", "claim_id": CLAIM_ID, "outcome": "accepted",
            "kind": "one_off", "reason": "correct for this panel", "by": "rafael",
            "at": "2026-08-24"}
    base.update(over)
    return MarkerDecision(**base)


def test_a_decision_names_the_claim_it_rules_on_and_reads_on_its_own():
    settled = decision()
    assert (settled.subject, str(settled.predicate), settled.value, settled.claim_id) == (
        "NAKATPASE", "segmentation_role", "cyto_structural", CLAIM_ID)
    assert "supported_by" not in MarkerDecision.model_fields


def test_there_is_no_undecided_decision():
    with pytest.raises(ValueError, match="outcome"):
        decision(outcome="proposed")
    assert not {"from_status", "to_status"} & set(MarkerDecision.model_fields)


def test_a_decision_outcome_is_not_optional():
    with pytest.raises(ValueError, match="outcome"):
        decision(outcome="")


def test_a_decision_kind_is_controlled():
    with pytest.raises(ValueError, match="kind"):
        decision(kind="maybe")


def test_an_acceptance_carries_no_kind():
    """`kind` says how a REJECTION generalises, so an acceptance leaves it blank."""
    assert decision(kind="").kind == ""


def test_a_decision_is_refused_without_a_decider():
    with pytest.raises(ValueError, match="decision by is blank"):
        decision(by="")
    with pytest.raises(ValueError, match="decision at is blank"):
        decision(at="")


def test_a_retried_decision_is_the_same_decision():
    assert decision().decision() == decision().decision()


@pytest.mark.parametrize("field, other", [
    ("claim_id", "something else"),
    ("outcome", "rejected"),
    ("kind", "rule"),
    ("reason", "wrong for this panel"),
    ("by", "someone else"),
    ("at", "2026-08-25"),
])
def test_a_different_decision_is_a_different_decision(field, other):
    assert decision().decision() != decision(**{field: other}).decision()


def test_what_the_claim_says_is_not_part_of_the_ruling():
    assert decision().decision() == decision(value="marker").decision()
