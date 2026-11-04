"""The composed reagent import gate, and the isoform check. Row sources are injected."""
from __future__ import annotations

import pytest
from clinicalomx_reagents.rows import antibodies

from clinicalomx_markers.domain.marker import MarkerClaim, MarkerPredicate
from clinicalomx_markers.domain.reagent_inventory import (
    conjugate_problems,
    validate_conjugates,
)
from clinicalomx_markers.seed_data.rows import markers


def _antibody(**overrides):
    row = {
        "catalogue_number": "4350003",
        "vendor": "Akoya Biosciences",
        "target_label": "E-Cadherin",
        "canonical_name": "E-CADHERIN",
        "uniprot_isoform": "",
        "bx_barcode": "BX001",
        "akoya_id": "AKY-001",
        "status": "draft",
    }
    return {**row, **overrides}


def _barcode(**overrides):
    """The reporter of the same akoya set as ``_antibody``."""
    row = {
        "rx_barcode": "RX001",
        "akoya_id": "AKY-001",
        "fluorophore": "ATTO550",
        "status": "draft",
    }
    return {**row, **overrides}


def _fluorophore(**overrides):
    row = {
        "canonical_name": "ATTO550",
        "excitation_nm": "554",
        "emission_nm": "576",
        "fluorofinder_url": "",
        "aliases": "",
        "status": "draft",
    }
    return {**row, **overrides}


def _marker(name="E-CADHERIN"):
    return {"canonical_name": name, "full_name": "", "status": "draft"}


def _target(value="single:P12830", subject="E-CADHERIN"):
    """A target claim built directly, so its value can be unreadable."""
    return MarkerClaim(subject=subject, predicate=MarkerPredicate.TARGET, value=value,
                       source="human", source_id="rafael", evidence="datasheet")


# --- the seeded inventory ---------------------------------------------------


def test_the_curated_inventory_validates():
    validate_conjugates()


def test_every_antibody_names_a_known_marker():
    known = {r["canonical_name"] for r in markers()}
    dangling = [a["catalogue_number"] for a in antibodies() if a["canonical_name"] not in known]
    assert not dangling, f"antibodies.csv references unknown markers: {dangling}"


# --- the pairing ------------------------------------------------------------


# --- barcode format ---------------------------------------------------------


# --- akoya_id ---------------------------------------------------------------


# --- what an import must refuse ---------------------------------------------


def test_a_blank_marker_reference_does_not_pass_as_known():
    """A blank canonical_name in markers.csv matches nothing."""
    problems = conjugate_problems(
        antibodies=[_antibody(canonical_name="")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        markers=[_marker(name="")],
    )
    assert any("unknown marker" in p for p in problems)


# --- the isoform ------------------------------------------------------------


def _common(**overrides):
    return {"fluorophores": [_fluorophore()], "barcodes": [_barcode()],
            "markers": [_marker()], **overrides}


def _isoform_problems(isoform, *targets):
    return conjugate_problems(antibodies=[_antibody(uniprot_isoform=isoform)],
                              targets=list(targets), **_common())


def test_a_blank_isoform_means_the_clone_is_not_isoform_specific():
    assert not _isoform_problems("", _target())


def test_an_isoform_of_the_markers_own_accession_passes():
    """P12830-2 is an isoform of E-CADHERIN's target P12830."""
    assert not _isoform_problems("P12830-2", _target())


def test_the_seeded_target_claims_are_what_the_check_reads_by_default():
    """E-CADHERIN is seeded as single:P12830 in targets.csv."""
    assert not conjugate_problems(antibodies=[_antibody(uniprot_isoform="P12830-2")],
                                  **_common())
    problems = conjugate_problems(antibodies=[_antibody(uniprot_isoform="P08575-8")],
                                  **_common())
    assert any("not among the target candidates" in p for p in problems)


def test_an_isoform_of_a_different_protein_fails_the_import():
    problems = _isoform_problems("P08575-8", _target())
    assert any("parent accession is not among the target candidates" in p for p in problems)


def test_any_candidate_of_a_multi_protein_set_can_parent_the_isoform():
    """Any candidate of a cross_reactive set can parent the isoform."""
    assert not _isoform_problems("P08575-8", _target("cross_reactive:P08575|P12830"))


def test_an_isoform_candidate_parents_an_isoform_of_the_same_protein():
    """Candidates are compared as gene products."""
    assert not _isoform_problems("P08575-8", _target("isoform_ambiguous:P08575-2|P08575-8"))


def test_another_markers_target_claim_does_not_parent_the_isoform():
    """Target claims are read per marker."""
    problems = _isoform_problems("P08575-8", _target(), _target("single:P08575", "CD45"))
    assert any("parent accession is not among the target candidates" in p for p in problems)


@pytest.mark.parametrize("raw", ["P12830", "P12830-", "-2", "notanaccession-2", "P12830-2-3"])
def test_a_value_that_is_not_an_isoid_is_rejected(raw):
    """A gene product is not an isoform of itself."""
    problems = _isoform_problems(raw, _target())
    assert any("expected a UniProt IsoId" in p for p in problems)


def test_an_unreadable_target_claim_is_named_rather_than_crashed_on():
    problems = _isoform_problems("P12830-2", _target("see the datasheet"))
    assert any("unreadable target claim 'see the datasheet'" in p for p in problems)
    assert any("has no readable target claim" in p for p in problems)


def test_a_marker_with_no_target_claim_cannot_back_an_isoform():
    """DAPI and BLANK target no protein."""
    problems = _isoform_problems("P12830-2")
    assert any("has no readable target claim" in p for p in problems)


# --- the akoya set ----------------------------------------------------------


def test_validate_reports_every_problem_at_once():
    with pytest.raises(ValueError) as excinfo:
        validate_conjugates(
            antibodies=[_antibody(bx_barcode="nope", canonical_name="CD9999")],
            barcodes=[_barcode()],
            fluorophores=[_fluorophore()],
            markers=[_marker()],
        )
    message = str(excinfo.value)
    assert "not a barcode" in message and "unknown marker" in message


# --- the report as a whole --------------------------------------------------


def test_the_report_is_pinned_message_for_message_and_in_order():
    """Every kind of finding, from both packages, pinned in order. A change here is a behaviour
    change."""
    def row(**cells):
        return cells

    problems = conjugate_problems(
        antibodies=[
            _antibody(catalogue_number="1", uniprot_isoform="P08575-8", bx_barcode="BX002",
                      akoya_id="AKY-002", vendor="", target_label=""),
            _antibody(catalogue_number="1", canonical_name="CD9999", bx_barcode="nope",
                      akoya_id="", status="bogus", vendor="", target_label=""),
            _antibody(catalogue_number="3", uniprot_isoform="P12830-2", bx_barcode="BX002",
                      akoya_id="AKY-003", vendor="", target_label=""),
            row(catalogue_number="", canonical_name="CD45", uniprot_isoform="P12830-2",
                bx_barcode="BX005", akoya_id="AKY-005", status="draft"),
        ],
        barcodes=[
            _barcode(rx_barcode="RX003", akoya_id="AKY-002"),
            _barcode(rx_barcode="RX003", akoya_id="AKY-004", fluorophore="NOPE"),
            _barcode(rx_barcode="zz", akoya_id="AKY-009"),
            _barcode(rx_barcode="RX002", akoya_id="AKY-007"),
        ],
        fluorophores=[_fluorophore(), _fluorophore(),
                      row(canonical_name="", status="draft")],
        markers=[_marker(), {"canonical_name": "CD45", "full_name": "", "status": "x"},
                 row(canonical_name="", status="draft")],
        targets=[_target(), _target("see datasheet", "CD45")],
    )
    assert problems == [
        "antibodies.csv: missing column 'vendor'",
        "antibodies.csv: missing column 'target_label'",
        "antibodies.csv: 1 has an empty akoya_id; write NA for a custom conjugation",
        "antibodies.csv: 1 has status 'bogus' not in (active, draft)",
        "antibodies.csv: row 4 has an empty catalogue_number",
        "fluorophores.csv: missing column 'excitation_nm'",
        "fluorophores.csv: missing column 'emission_nm'",
        "fluorophores.csv: missing column 'fluorofinder_url'",
        "fluorophores.csv: missing column 'aliases'",
        "fluorophores.csv: row 3 has an empty canonical_name",
        "markers.csv: missing column 'full_name'",
        "markers.csv: CD45 has status 'x' not in (active, draft)",
        "markers.csv: row 3 has an empty canonical_name",
        "fluorophores.csv: duplicate canonical_name 'ATTO550'",
        "barcodes.csv: RX003 names unknown fluorophore 'NOPE'",
        "barcodes.csv: zz: 'zz' is not a barcode: expected BX or RX followed by digits",
        "barcodes.csv: duplicate rx_barcode 'RX003'",
        "antibodies.csv: 1: 'nope' is not a barcode: expected BX or RX followed by digits",
        "antibodies.csv: 1 names unknown marker 'CD9999'",
        "antibodies.csv: duplicate bx_barcode 'BX002'",
        "antibodies.csv: duplicate catalogue_number '1'",
        "antibodies.csv: 1 carries isoform 'P08575-8' whose parent accession is not among "
        "the target candidates of marker 'E-CADHERIN': P12830",
        "antibodies.csv: row 4 carries isoform 'P12830-2' but marker 'CD45' has an "
        "unreadable target claim 'see datasheet': 'see datasheet' is not a target set: "
        "expected <kind>:<candidates>",
        "antibodies.csv: row 4 carries isoform 'P12830-2' but marker 'CD45' has no "
        "readable target claim, so its parent accession cannot be checked",
        "antibodies.csv/barcodes.csv: akoya_id 'AKY-002' names one set but covers "
        "mismatched barcodes: BX002, RX003",
        "antibodies.csv/barcodes.csv: BX002 carries akoya_id 'AKY-002' but its reporter "
        "carries 'AKY-007': one set carries one id",
        "antibodies.csv/barcodes.csv: BX002 carries akoya_id 'AKY-003' but its reporter "
        "carries 'AKY-007': one set carries one id",
    ]
