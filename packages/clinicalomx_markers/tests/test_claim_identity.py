"""A claim's stored identity, pinned to literal values.

`Claim.claim_id` is content-addressed and stored. Changing `digest`, `DIGEST_CHARS`,
`NAME_LIMIT`, the field order or the separator orphans every stored citation. A failure here
means the identity rule moved: revert it, or migrate the stored rows before updating a literal.
Every expected value is a literal, never computed by the code under test.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.model import (
    DIGEST_CHARS,
    NAME_LIMIT,
    digest,
    short_id,
)

from clinicalomx_markers.domain.marker import (
    MarkerClaim,
    MarkerDecision,
    MarkerPredicate,
    MarkerSource,
)
from clinicalomx_markers.domain.reagent_mapping import (
    MappingClaim,
    ReagentMappingPredicate,
    ReagentMappingSource,
)

#: The claim each id case varies from.
CLAIM = {
    "subject": "CD8A",
    "predicate": MarkerPredicate.SEGMENTATION_ROLE,
    "value": "marker",
    "source": MarkerSource.UNIPROT,
    "source_id": "P01732",
    "evidence": "Cell membrane.",
}


def claim(**overrides: str) -> MarkerClaim:
    return MarkerClaim(**{**CLAIM, **overrides})


# --- digest ------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    # sha256 of nothing; a blank field digests to this.
    ("", "e3b0c44298fc"),
    ("DAPI", "5691fe3bf99d"),
    # `claim_id` digests `value@source_version`.
    ("Nucleus@", "c7b2aa248ec4"),
    ("Nucleus@2026_03", "61c9d6e77298"),
    # Encoded before hashing, so non-ASCII has one answer.
    ("Zellkern, Zellmembran äöü", "ff48b15bdbf5"),
    ("α-SMA", "22d3663f4062"),
])
def test_digest_is_pinned(text: str, expected: str):
    assert digest(text) == expected


def test_a_digest_is_twelve_hex_characters():
    assert DIGEST_CHARS == 12
    assert digest("") == "e3b0c44298fc"
    assert len(digest("anything at all")) == 12


# --- claim_id ----------------------------------------------------------------------------

def test_claim_id_with_a_blank_source_version():
    """A human assertion has no upstream release."""
    assert claim().source_version == ""
    assert claim().claim_id() == "CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5"


def test_claim_id_with_a_populated_source_version():
    """A later release restating the same value is a distinct claim."""
    assert (claim(source_version="2026_03").claim_id()
            == "CD8A:segmentation_role:uniprot:P01732:8a9a08594074")


def test_claim_id_when_the_value_holds_the_separator():
    """The value is digested, so a colon in it adds no field."""
    assert (claim(value="marker:nucleus").claim_id()
            == "CD8A:segmentation_role:uniprot:P01732:5bd4485bdf40")


def test_claim_id_when_the_subject_holds_the_separator():
    """The subject is not digested, so its colon lands in the id verbatim."""
    got = claim(subject="CD8A:PCF").claim_id()
    assert got == "CD8A:PCF:segmentation_role:uniprot:P01732:334e3d7d8bf5"
    assert got.rsplit(":", 1)[1] == "334e3d7d8bf5"


def test_the_field_order_is_subject_predicate_source_source_id_digest():
    assert claim().claim_id().split(":") == [
        "CD8A", "segmentation_role", "uniprot", "P01732", "334e3d7d8bf5"]


def test_a_second_domains_claim_id_is_pinned_by_the_same_rule():
    mapping = MappingClaim(
        subject="akoya-panel-2026-03.xlsx",
        predicate=ReagentMappingPredicate.RX_BARCODE,
        value="Reporter Barcode",
        source=ReagentMappingSource.SPREADSHEET,
        source_id="sheet1!C1",
        evidence="header 'Reporter Barcode'",
    )
    assert (mapping.claim_id()
            == "akoya-panel-2026-03.xlsx:rx_barcode:spreadsheet:sheet1!C1:3d36959d789f")


# --- short_id ----------------------------------------------------------------------------

#: Two ids differing only in their last characters, well past `NAME_LIMIT`.
LONG_STEM = "IMAGING-CHANNEL-" + "OPAL520-" * 20

#: `LONG_STEM` cut where `short_id` cuts, written as the literal 137 characters.
CUT_STEM = ("IMAGING-CHANNEL-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-"
            "OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-OPAL520-O")


def test_an_id_under_the_limit_is_returned_unchanged():
    ordinary = "CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5"
    assert len(ordinary) < NAME_LIMIT
    assert short_id(ordinary) == "CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5"


def test_the_limit_itself_is_not_shortened():
    assert NAME_LIMIT == 150
    at_limit = "a" * 150
    assert short_id(at_limit) == at_limit
    assert short_id("a" * 151) == "a" * 137 + ":5416d58d0995"


def test_an_id_over_the_limit_is_cut_and_digested():
    assert len(LONG_STEM + ":one") > NAME_LIMIT
    assert len(CUT_STEM) == 137
    assert short_id(LONG_STEM + ":one") == CUT_STEM + ":614d7e7bbafc"


def test_two_ids_agreeing_up_to_the_cut_do_not_collide():
    """Two ids sharing the first 137 characters store as two names: the tail is digested."""
    one, two = LONG_STEM + ":one", LONG_STEM + ":two"
    assert one[:137] == two[:137] == CUT_STEM

    assert short_id(one) == CUT_STEM + ":614d7e7bbafc"
    assert short_id(two) == CUT_STEM + ":17425f27d6b5"
    assert short_id(one) != short_id(two)
    assert len(short_id(one)) == len(short_id(two)) == NAME_LIMIT


def test_a_caller_supplied_limit_is_pinned_too():
    """A caller-supplied limit is pinned too."""
    assert (short_id("DAPI:subcellular_location:human:rafael:c7b2aa248ec4", limit=30)
            == "DAPI:subcellular_:fd8fcb56625a")


# --- Decision ----------------------------------------------------------------------------

def test_decision_is_pinned_field_by_field():
    """`Decision.decision`, field by field: `gate.decisions_for` compares it."""
    decided = MarkerDecision(
        subject="CD8A",
        predicate=MarkerPredicate.SEGMENTATION_ROLE,
        value="marker",
        claim_id="CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5",
        outcome="accepted",
        by="rafael",
        at="2026-08-24",
    )
    assert decided.decision() == (
        "CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5", "accepted", "", "",
        "rafael", "2026-08-24",
    )


def test_confidence_is_outside_the_claim_id():
    """Confidence is stored beside the claim, never in its identity."""
    assert claim(confidence=0.9).claim_id() == claim().claim_id() == (
        "CD8A:segmentation_role:uniprot:P01732:334e3d7d8bf5")
