"""Supersession and source invalidation, offline.

A source restating a subject and predicate replaces what it said before. Two sources
disagreeing both stand. A `SourceInvalidation` voids a source with nothing to replace it.
Both backends apply the same `active` rule; the ledger half runs in `tests_pg/`.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import (
    Assertion,
    active,
    standing,
    supersession_key,
)
from clinicalomx_ledger.grammar.store import (
    MemoryClaimStore,
)

from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MARKER_TYPES,
    MarkerClaim,
    MarkerPredicate,
    MarkerSourceInvalidation,
)
from clinicalomx_markers.domain.reagent_mapping import (
    REAGENT_MAPPING_DOMAIN,
    MappingSourceInvalidation,
)

APPROVAL = Judgement(by="rafael", at="2026-08-27")


def make(**over) -> MarkerClaim:
    """One unreviewed localisation claim."""
    base = {
        "subject": "NAKATPASE",
        "predicate": MarkerPredicate.SUBCELLULAR_LOCATION,
        "value": "Cell membrane",
        "source": "uniprot",
        "source_id": "P05023",
        "source_version": "2024_03",
        "evidence": "Cell membrane; Multi-pass membrane protein.",
    }
    base.update(over)
    return MarkerClaim(**base)


def store(**over) -> MemoryClaimStore:
    return MemoryClaimStore(MARKER_DOMAIN, **over)


def void(**over) -> MarkerSourceInvalidation:
    base = {"source": "uniprot", "source_id": "P05023",
            "reason": "the release shifted the subcellular location column",
            "by": "rafael", "at": "2026-08-27"}
    base.update(over)
    return MarkerSourceInvalidation(**base)


# --- the rule, with no backend under it ---------------------------------------------------

def test_the_key_is_the_source_and_not_only_the_assertion():
    """Supersession groups on an `Assertion` that includes the source."""
    assert supersession_key(make()) == Assertion(
        "NAKATPASE", "subcellular_location", "uniprot", "P05023")


def test_the_key_holds_plain_strings_and_not_enum_members():
    """The key holds plain strings, matching keys read back from the instance."""
    key = supersession_key(make())
    assert [type(part) for part in key] == [str] * len(key)


def test_a_source_restating_itself_supersedes_and_two_sources_do_not():
    a = Assertion("NAKATPASE", "subcellular_location", "uniprot", "P05023")
    b = Assertion("NAKATPASE", "subcellular_location", "pubmed", "PMID:1")

    assert standing([("first", a, ""), ("second", b, ""), ("third", a, "")]) == [
        "second", "third"]


def test_the_survivors_come_back_in_creation_order():
    """Survivors keep creation order: consumers read the last entry as the latest."""
    a = Assertion("NAKATPASE", "subcellular_location", "uniprot", "P05023")
    b = Assertion("NAKATPASE", "go_cc", "uniprot", "P05023")

    assert standing([(0, a, ""), (1, b, ""), (2, a, "")]) == [1, 2]


def test_a_void_is_applied_before_supersession_and_not_after():
    """A bounded void is applied before supersession, so the earlier release stands again."""
    a = Assertion("NAKATPASE", "subcellular_location", "uniprot", "P05023")
    rows = [("old", a, "2024_03"), ("new", a, "2026_03")]

    assert standing(rows) == ["new"]
    assert standing(rows, [void(source_version="2026_03")]) == ["old"]


def test_active_keeps_two_byte_identical_claims_apart():
    """Two byte-identical claims both survive: handles are positions."""
    same = [make(), make()]

    assert active(same) == [same[-1]]
    assert len(active(same)) == 1


# --- SourceInvalidation, the model --------------------------------------------------------

@pytest.mark.parametrize("blank", ["reason", "by", "at"])
@pytest.mark.parametrize("emptied", ["", "   "])
def test_an_invalidation_is_refused_without_a_human_a_date_and_a_reason(blank, emptied):
    """Whitespace counts as blank, and is refused, not stripped."""
    with pytest.raises(ValueError, match=f"invalidation {blank} is blank"):
        void(**{blank: emptied})


def test_an_invalidation_refuses_a_source_outside_its_domain_vocabulary():
    with pytest.raises(ValueError, match="source"):
        MarkerSourceInvalidation(source="spreadsheet", source_id="sheet1!C1",
                                 reason="shifted column", by="rafael", at="2026-08-27")


def test_a_bounded_invalidation_names_one_release_and_an_unbounded_one_names_all():
    read = Assertion("NAKATPASE", "subcellular_location", "uniprot", "P05023")

    assert void(source_version="2026_03").voids(read, "2026_03")
    assert not void(source_version="2026_03").voids(read, "2024_03")
    assert void().voids(read, "2024_03") and void().voids(read, "2026_03")


def test_an_invalidation_does_not_reach_another_source_id():
    read = Assertion("VIMENTIN", "subcellular_location", "uniprot", "P08670")

    assert not void().voids(read, "2024_03")


def test_a_store_refuses_another_domain_s_invalidation():
    """Both source vocabularies spell `human`, so a store checks the invalidation's domain."""
    with pytest.raises(ValueError, match="MarkerSourceInvalidation"):
        store().invalidate(MappingSourceInvalidation(
            source="spreadsheet", source_id="sheet1!C1", reason="shifted column",
            by="rafael", at="2026-08-27"))


# --- the store, through the memory backend ------------------------------------------------

def test_a_second_claim_from_one_source_makes_the_first_inactive():
    s = store()
    first = s.add(make())
    second = s.add(make(value="Basolateral cell membrane"))

    assert s.held("NAKATPASE") == [second]
    assert s.held("NAKATPASE", history=True) == [first, second]


def test_two_sources_disagreeing_are_both_active():
    s = store()
    s.add(make())
    s.add(make(source="pubmed", source_id="PMID:1", source_version="",
               value="Basolateral only"))

    assert sorted(str(c.source) for c in s.held("NAKATPASE")) == ["pubmed", "uniprot"]


def test_supersession_is_per_predicate_as_well_as_per_source():
    """One source saying two things about one subject is two slots."""
    s = store()
    location = s.add(make())
    function = s.add(make(predicate=MarkerPredicate.FUNCTION, value="ion transport"))

    assert s.held("NAKATPASE") == [location, function]
    assert s.held("NAKATPASE", predicate=MarkerPredicate.FUNCTION) == [function]


def test_an_invalidated_source_stops_standing_and_stays_in_the_log():
    s = store()
    written = s.add(make())
    s.add(make(source="pubmed", source_id="PMID:1", source_version="",
               value="Basolateral only"))

    s.invalidate(void())

    assert [str(c.source) for c in s.held("NAKATPASE")] == ["pubmed"]
    assert written in s.held("NAKATPASE", history=True)
    assert s.claims == s.held("NAKATPASE", history=True), "nothing was deleted"


def test_a_bounded_invalidation_leaves_a_later_release_alone():
    s = store()
    s.add(make(source_version="2026_03", value="Nucleus"))
    reread = s.add(make(source_version="2026_05", value="Cell membrane"))

    s.invalidate(void(source_version="2026_03"))

    assert s.held("NAKATPASE") == [reread]


def test_a_bounded_invalidation_restores_the_release_before_it():
    s = store()
    earlier = s.add(make(source_version="2024_03"))
    s.add(make(source_version="2026_03", value="Nucleus"))

    s.invalidate(void(source_version="2026_03"))

    assert s.held("NAKATPASE") == [earlier]


def test_a_second_invalidation_of_one_source_is_a_second_record():
    """A second invalidation is a second record."""
    s = store()
    s.invalidate(void())
    s.invalidate(void(by="wenchao", reason="confirmed against the 2026_05 release"))

    assert [v.by for v in s.invalidations()] == ["rafael", "wenchao"]


def test_a_superseded_claim_is_not_a_duplicate_and_lands_a_row():
    """`stored_duplicate` compares against the standing claim only."""
    s = store()
    first = s.add(make())
    s.add(make(value="Nucleus"))
    again = s.add(make())

    assert again is not first, "the repeat is a new row, not the stored one"
    assert len(s.held("NAKATPASE", history=True)) == 3
    assert s.held("NAKATPASE") == [again]


def test_the_standing_claim_repeated_still_lands_no_row():
    s = store()
    first = s.add(make())
    repeat = s.add(make())

    assert repeat is first
    assert s.held("NAKATPASE", history=True) == [first]


def test_a_rival_source_standing_beside_a_claim_does_not_hide_the_repeat():
    """A rival source standing beside a claim does not make its repeat a new row."""
    s = store()
    first = s.add(make())
    rival = s.add(make(source="pubmed", source_id="PMID:1", source_version="",
                       value="Basolateral cell membrane", evidence="Basolateral only."))
    repeat = s.add(make())

    assert repeat is first, "the standing claim from its own source, not a new row"
    assert s.held("NAKATPASE", history=True) == [first, rival]


def test_the_review_queue_is_untouched_by_the_projection():
    """Decisions are unaffected: the accepted superseded claim stays the answer until ruled on."""
    s = store(subject_types={"NAKATPASE-PCF": MARKER_TYPES.IMAGING_CHANNEL})
    role = {"subject": "NAKATPASE-PCF", "predicate": MarkerPredicate.SEGMENTATION_ROLE}
    s.add(make(**role, value="marker"), outcome="accepted", judgement=APPROVAL)
    s.add(make(**role, value="cyto_structural"), outcome="accepted", judgement=APPROVAL)

    assert len(s.held("NAKATPASE-PCF")) == 1
    assert len(s.decisions("NAKATPASE-PCF")) == 2
    assert s.in_state("accepted") == ["NAKATPASE-PCF"]
    assert sorted(d.value for d in s.answers("NAKATPASE-PCF")) == ["cyto_structural", "marker"]


def test_the_second_domain_projects_the_same_way():
    sheet = "akoya-panel-2026-03.xlsx"
    claims = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={sheet: "Artifact"})
    base = {"subject": sheet, "predicate": "rx_barcode", "source": "spreadsheet",
            "source_id": "sheet1!C1"}
    claims.add(REAGENT_MAPPING_DOMAIN.claim(
        **base, value="Reporter Barcode", evidence="header reads Reporter Barcode"))
    corrected = claims.add(REAGENT_MAPPING_DOMAIN.claim(
        **base, value="RX Barcode", evidence="header reads RX Barcode after the re-export"))

    assert claims.held(sheet) == [corrected]
    assert len(claims.held(sheet, history=True)) == 2
