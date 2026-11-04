"""`Claim` and `Decision` are generic over their vocabularies, checked on both domains.

The base `StrEnum`s are empty: Python refuses to subclass an enum that has members.
"""
from __future__ import annotations

from enum import StrEnum

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import (
    Claim,
    ClaimDomain,
    ClaimPredicate,
    ClaimSource,
    Decision,
)
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MarkerClaim,
    MarkerDecision,
    MarkerPredicate,
    MarkerSource,
)
from clinicalomx_markers.domain.reagent_mapping import (
    REAGENT_MAPPING_DOMAIN,
    MappingClaim,
    MappingDecision,
    ReagentMappingPredicate,
    ReagentMappingSource,
)

DOMAINS = [MARKER_DOMAIN, REAGENT_MAPPING_DOMAIN]

#: One valid claim per domain, as keyword arguments.
EXAMPLES = {
    MARKER_DOMAIN: {
        "subject": "CD8A", "predicate": MarkerPredicate.SEGMENTATION_ROLE,
        "value": "marker", "source": MarkerSource.UNIPROT, "source_id": "P01732",
        "evidence": "Cell membrane."},
    REAGENT_MAPPING_DOMAIN: {
        "subject": "akoya-panel-2026-03.xlsx",
        "predicate": ReagentMappingPredicate.RX_BARCODE,
        "value": "Reporter Barcode", "source": ReagentMappingSource.SPREADSHEET,
        "source_id": "sheet1!C1",
        "evidence": "header 'Reporter Barcode'; 28/28 values match RX[0-9]{3}"},
}


def ids(domain: ClaimDomain) -> str:
    return domain.claim.__name__


# --- the abstract bases -----------------------------------------------------------------

def test_the_base_vocabularies_have_no_members():
    assert list(ClaimSource) == []
    assert list(ClaimPredicate) == []


def test_an_empty_enum_can_be_extended_and_a_populated_one_cannot():
    class Fresh(ClaimSource):
        ONE = "one"

    assert list(Fresh) == [Fresh.ONE]
    with pytest.raises(TypeError, match="cannot extend"):
        class Wider(Fresh):
            TWO = "two"


def test_the_unparameterised_base_accepts_nothing():
    with pytest.raises(ValueError) as raised:
        Claim(subject="CD8A", predicate="segmentation_role", value="marker",
              source="uniprot", source_id="P01732", evidence="x")

    message = str(raised.value)
    assert "predicate" in message and "source" in message


# --- what parameterising buys -----------------------------------------------------------

@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_a_domain_accepts_its_own_vocabulary(domain):
    built = domain.claim(**EXAMPLES[domain])
    assert isinstance(built, domain.claim)
    assert built.predicate is EXAMPLES[domain]["predicate"]
    assert built.source is EXAMPLES[domain]["source"]


@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_a_domain_accepts_the_raw_string(domain):
    """The ledger stores plain strings, so a parameterised model accepts them."""
    plain = dict(EXAMPLES[domain])
    plain["predicate"] = str(plain["predicate"])
    plain["source"] = str(plain["source"])

    built = domain.claim(**plain)

    assert built.predicate is EXAMPLES[domain]["predicate"]
    assert built.source is EXAMPLES[domain]["source"]


@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_a_domain_refuses_the_other_domain_by_name(domain):
    other = next(d for d in DOMAINS if d is not domain)
    for field in ("predicate", "source"):
        wrong = dict(EXAMPLES[domain])
        wrong[field] = EXAMPLES[other][field]
        with pytest.raises(ValueError, match=field):
            domain.claim(**wrong)


@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_a_domain_refuses_nonsense(domain):
    for field in ("predicate", "source"):
        wrong = dict(EXAMPLES[domain], **{field: "not-a-thing"})
        with pytest.raises(ValueError, match=field):
            domain.claim(**wrong)


def test_the_typevar_bound_is_not_enforced_at_runtime():
    """pydantic does not check the TypeVar bound; a type checker does."""
    class Unrelated(StrEnum):
        X = "x"

    Odd = Claim[Unrelated, Unrelated]
    built = Odd(subject="s", predicate="x", value="v", source="x", source_id="1",
                evidence="e")

    assert built.predicate is Unrelated.X


# --- templated descriptions ---------------------------------------------------------------

@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_every_description_substitutes_the_domains_entity(domain):
    """Descriptions are the JSON schema a model reads, so each names its domain's entity."""
    for model in (domain.claim, domain.decision):
        for name, field in model.model_fields.items():
            assert "{" not in (field.description or ""), \
                f"{model.__name__}.{name} still carries its template"
            assert "}" not in (field.description or "")


def test_the_base_keeps_its_template_so_each_domain_substitutes_independently():
    """pydantic copies `FieldInfo` per subclass, so the base keeps its template."""
    assert "{entity}" in Claim.model_fields["subject"].description
    assert MarkerClaim.model_fields["subject"].description == "The Marker this claim is about."
    assert MappingClaim.model_fields["subject"].description == (
        "The Spreadsheet this claim is about.")


def test_the_entity_reaches_the_schema_an_llm_reads():
    """`model_rebuild` puts the entity into the generated schema."""
    schema = MappingClaim.model_json_schema()
    assert schema["properties"]["subject"]["description"] == (
        "The Spreadsheet this claim is about.")


# --- the two constants the generic layer cannot hardcode ----------------------------------

def test_the_two_domains_disagree_about_both_parameters():
    assert MARKER_DOMAIN.subject_types == ("Marker", "ImagingChannel")
    assert REAGENT_MAPPING_DOMAIN.subject_types == ("Artifact",)

    assert len(MARKER_DOMAIN.reviewed_predicates) == 3
    assert len(list(MarkerPredicate)) == 7, "the marker domain reviews three of its seven"
    assert (set(REAGENT_MAPPING_DOMAIN.reviewed_predicates)
            == set(ReagentMappingPredicate)), "the mapping domain reviews all of its own"


def test_the_mapping_domain_reviews_a_predicate_added_later():
    """Every mapping predicate is reviewed: spelled as the whole enum."""
    from clinicalomx_markers.domain import reagent_mapping as model

    assert model.REVIEWED_PREDICATES == tuple(ReagentMappingPredicate)


def test_a_domain_states_every_value_the_machinery_cannot_know():
    """Every field is required."""
    with pytest.raises(ValueError):
        ClaimDomain(claim=MarkerClaim, decision=MarkerDecision)


def test_a_domain_refuses_a_value_it_does_not_declare():
    with pytest.raises(ValueError):
        ClaimDomain(
            claim=MARKER_DOMAIN.claim,
            decision=MARKER_DOMAIN.decision,
            subject_types=MARKER_DOMAIN.subject_types,
            reviewed_predicates=MARKER_DOMAIN.reviewed_predicates,
            claim_type=MARKER_DOMAIN.claim_type,
            verdict_type=MARKER_DOMAIN.verdict_type,
            state_feature=MARKER_DOMAIN.state_feature,
            subject_type="Marker",
        )


@pytest.mark.parametrize("domain", DOMAINS, ids=ids)
def test_a_domain_pairs_its_own_models(domain):
    assert issubclass(domain.claim, Claim)
    assert issubclass(domain.decision, Decision)
    assert domain.claim.entity == domain.decision.entity


# --- the second consumer, end to end ------------------------------------------------------

def test_the_spreadsheet_domain_runs_the_whole_machinery():
    """A CSV column mapping, from proposal to decision. Subject: the sheet. Predicate: the field.
    Value: the column."""
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN,
                              subject_types={sheet: "Artifact"})

    proposed = claims.add(MappingClaim(**EXAMPLES[REAGENT_MAPPING_DOMAIN], confidence=0.9))
    assert claims.in_state("unresolved") == [sheet]
    assert claims.answers(sheet) == [], "a proposal is not an answer"

    claims.add(MappingClaim(**EXAMPLES[REAGENT_MAPPING_DOMAIN], confidence=0.9),
               outcome="accepted",
               judgement=Judgement(by="rafael", at="2026-08-26",
                                   reason="header and value shape agree"))

    assert claims.in_state("accepted") == [sheet]
    assert claims.held(sheet) == [proposed], "one assertion, decided once"
    assert claims.held(sheet)[0].confidence == 0.9
    (settled,) = claims.answers(sheet)
    assert isinstance(settled, MappingDecision)
    assert (settled.predicate, settled.value) == ("rx_barcode", "Reporter Barcode")
    assert settled.by == "rafael"


def test_two_sources_agreeing_are_both_the_answer():
    """Two claims, each accepted by its own decision, are both the answer."""
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={sheet: "Artifact"})

    base = {"subject": sheet, "predicate": ReagentMappingPredicate.FLUOROPHORE,
            "value": "ATTO550"}
    from_sheet = claims.add(MappingClaim(
        **base, source=ReagentMappingSource.SPREADSHEET, source_id="sheet1!E4",
        evidence="column 'Dye' reads ATTO550"))
    from_image = claims.add(MappingClaim(
        **base, source=ReagentMappingSource.IMAGE_METADATA, source_id="qptiff:channel-4",
        evidence="channel name ATTO550 in the qptiff header"))

    signed = Judgement(by="rafael", at="2026-08-26", reason="both sources agree")
    for agreeing in (from_sheet, from_image):
        claims.decide(agreeing, outcome="accepted", judgement=signed)

    settled = claims.answers(sheet, predicate=ReagentMappingPredicate.FLUOROPHORE)
    assert {d.claim_id for d in settled} == {from_sheet.claim_id(), from_image.claim_id()}
    assert {d.value for d in settled} == {"ATTO550"}


def test_a_mapping_subject_must_still_be_a_record():
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN,
                              subject_types={"some-column": "Column"})

    with pytest.raises(ValueError, match="must be a Artifact"):
        claims.add(MappingClaim(**dict(EXAMPLES[REAGENT_MAPPING_DOMAIN],
                                       subject="some-column")))


def test_the_marker_domains_claim_cannot_be_stored_by_the_mapping_domains_store():
    """A store refuses another domain's claim, even as a plain proposal."""
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={"CD8A": "Artifact"})
    foreign = MarkerClaim(**EXAMPLES[MARKER_DOMAIN])

    with pytest.raises(ValueError, match="MappingClaim"):
        claims.add(foreign)

    with pytest.raises(ValueError, match="MappingClaim"):
        claims.add(foreign, outcome="accepted",
                   judgement=Judgement(by="rafael", at="2026-08-26"))

    assert claims.claims == [], "and nothing about it was written either way"
    assert claims.in_state("unresolved") == []


def test_a_subject_stays_in_the_queue_until_every_reviewed_predicate_is_decided():
    """A subject leaves the queue only when every reviewed predicate is decided."""
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={sheet: "Artifact"})
    barcode = MappingClaim(**EXAMPLES[REAGENT_MAPPING_DOMAIN])
    dye = MappingClaim(**dict(EXAMPLES[REAGENT_MAPPING_DOMAIN],
                              predicate=ReagentMappingPredicate.FLUOROPHORE,
                              value="Fluorophore", source_id="sheet1!D1",
                              evidence="header 'Fluorophore'; 28/28 values are ATTO or Opal"))

    claims.add(barcode)
    claims.add(dye)
    assert claims.in_state("unresolved") == [sheet]

    claims.add(dye, outcome="accepted", judgement=Judgement(by="rafael", at="2026-08-26"))
    assert [d.outcome for d in claims.decisions(sheet, predicate="fluorophore")] == ["accepted"]
    assert claims.decisions(sheet, predicate="rx_barcode") == []
    assert claims.in_state("unresolved") == [sheet], "the undecided column keeps it queued"

    claims.add(barcode, outcome="accepted", judgement=Judgement(by="rafael", at="2026-08-26"))
    assert claims.in_state("unresolved") == []
    assert claims.in_state("accepted") == [sheet]


def test_a_rejection_of_the_last_undecided_predicate_also_clears_the_queue():
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={sheet: "Artifact"})
    barcode = MappingClaim(**EXAMPLES[REAGENT_MAPPING_DOMAIN])
    dye = MappingClaim(**dict(EXAMPLES[REAGENT_MAPPING_DOMAIN],
                              predicate=ReagentMappingPredicate.FLUOROPHORE,
                              value="Fluorophore", source_id="sheet1!D1",
                              evidence="header 'Fluorophore'; 28/28 values are ATTO or Opal"))
    signed = Judgement(by="rafael", at="2026-08-26")

    claims.add(barcode)
    claims.add(dye)
    claims.add(barcode, outcome="accepted", judgement=signed)
    assert claims.in_state("unresolved") == [sheet]

    claims.add(dye, outcome="rejected",
               judgement=Judgement(by="rafael", at="2026-08-26", kind="one_off",
                                   reason="that column is the reporter, not the dye"))
    assert claims.in_state("unresolved") == []
    assert claims.in_state("rejected") == [sheet]


def test_the_marker_domains_single_reviewed_predicate_is_unchanged():
    """With one reviewed predicate, the subject takes that predicate's state."""
    claims = MemoryClaimStore(MARKER_DOMAIN, subject_types={"CD8A": "Marker"})
    role = MarkerClaim(**EXAMPLES[MARKER_DOMAIN])

    claims.add(role)
    assert claims.in_state("unresolved") == ["CD8A"]

    claims.add(role, outcome="accepted", judgement=Judgement(by="rafael", at="2026-08-26"))
    assert claims.in_state("unresolved") == []
    assert claims.in_state("accepted") == ["CD8A"]

    claims.add(MarkerClaim(**dict(EXAMPLES[MARKER_DOMAIN], value="nucleus")),
               outcome="rejected",
               judgement=Judgement(by="rafael", at="2026-08-27", kind="one_off",
                                   reason="CD8A is not nuclear"))
    assert claims.in_state("unresolved") == []
    assert claims.in_state("accepted") == []
    assert claims.in_state("rejected") == ["CD8A"]
    assert [d.value for d in claims.answers("CD8A")] == ["marker"]


def test_the_four_settled_rules_hold_in_the_second_domain_too():
    """A re-decision is a second decision; an identical repeat is a no-op; a decision needs a
    decider; a proposal writes no decision."""
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={sheet: "Artifact"})
    mapping = MappingClaim(**EXAMPLES[REAGENT_MAPPING_DOMAIN])
    first = Judgement(by="rafael", at="2026-08-26")

    claims.add(mapping)                                            # a proposal decides nothing
    assert claims.decisions(sheet) == []

    claims.add(mapping, outcome="accepted", judgement=first)
    claims.add(mapping, outcome="accepted", judgement=first)       # identical repeat: no-op
    assert len(claims.decisions(sheet)) == 1

    claims.add(mapping, outcome="accepted",
               judgement=Judgement(by="wenchao", at="2026-08-27"))  # a second decision
    assert [d.by for d in claims.decisions(sheet)] == ["rafael", "wenchao"]

    with pytest.raises(ValueError, match="needs a judgement"):      # refused without a decider
        claims.add(mapping, outcome="accepted")
    assert len(claims.decisions(sheet)) == 2, "and the decisions are untouched by the refusal"
