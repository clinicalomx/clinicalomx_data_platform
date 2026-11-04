"""The reagent inventory and its import checks. Row sources are injected, never written to disk.

The isoform check and the composed import gate are tested in `clinicalomx_markers`.
"""
from __future__ import annotations

import pytest

from clinicalomx_reagents.inventory import (
    akoya_set_problems,
    file_problems,
    is_custom_conjugation,
    key_problems,
    pairs,
    parse_barcode,
    reporter_for,
)
from clinicalomx_reagents.rows import antibodies, barcodes, fluorophores
from clinicalomx_reagents.spec import NO_AKOYA_ID

KNOWN = {"E-CADHERIN"}


def _problems(*, antibodies, barcodes, fluorophores, known_markers):
    """Every reagent-side check, in the order `conjugate_problems` runs them."""
    return (file_problems(antibodies, barcodes, fluorophores)
            + key_problems(antibodies, barcodes, fluorophores, known_markers)
            + akoya_set_problems(antibodies, barcodes))


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
    """The reporter of the same akoya set as ``_antibody``, so the default pair is valid."""
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

# --- the seeded inventory ---------------------------------------------------


def test_the_curated_inventory_is_populated():
    assert antibodies(), "antibodies.csv is empty"
    assert barcodes(), "barcodes.csv is empty"
    assert fluorophores(), "fluorophores.csv is empty"


def test_no_fluorophore_url_is_written_unverified():
    """Spectra URLs stay empty until verified."""
    for row in fluorophores():
        url = row["fluorofinder_url"].strip()
        assert not url or url.startswith("http"), f"{row['canonical_name']}: bad url {url!r}"


def test_the_whole_inventory_is_still_draft():
    for rows, key in ((antibodies(), "catalogue_number"),
                      (barcodes(), "rx_barcode"),
                      (fluorophores(), "canonical_name")):
        active = [r[key] for r in rows if r["status"].strip() == "active"]
        assert not active, f"conjugate rows are now active: {active} - review this test"


# --- the pairing ------------------------------------------------------------


def test_matching_numbers_pair():
    assert pairs("BX002", "RX002")


def test_mismatched_numbers_do_not_pair():
    assert not pairs("BX002", "RX003")


def test_the_pairing_refuses_two_barcodes_of_the_same_kind():
    with pytest.raises(ValueError, match="expected RX"):
        pairs("BX002", "BX002")
    with pytest.raises(ValueError, match="expected BX"):
        pairs("RX002", "RX002")


def test_the_reporter_is_derived_not_stored():
    row = reporter_for("BX003", barcodes=[_barcode(rx_barcode="RX003", fluorophore="X")])
    assert row is not None and row["rx_barcode"] == "RX003"


def test_an_antibody_with_no_stocked_reporter_is_not_an_error():
    assert reporter_for("BX999", barcodes=barcodes()) is None


def test_the_seeded_antibodies_pair_with_the_seeded_reporters():
    """Where a reporter is stocked the numbers match. Not every antibody has one."""
    for row in antibodies():
        reporter = reporter_for(row["bx_barcode"])
        if reporter is None:
            continue
        assert pairs(row["bx_barcode"], reporter["rx_barcode"])


def test_one_unreadable_reporter_row_does_not_hide_the_valid_match():
    """An unreadable barcodes.csv row does not break other lookups."""
    stocked = _barcode(rx_barcode="RX002")
    rows = [_barcode(rx_barcode="R X005"), stocked]
    assert reporter_for("BX002", barcodes=rows) is stocked


def test_a_lookup_with_a_malformed_antibody_barcode_still_raises():
    with pytest.raises(ValueError, match="not a barcode"):
        reporter_for("nope", barcodes=[_barcode()])


# --- barcode format ---------------------------------------------------------


@pytest.mark.parametrize("raw,number", [("BX002", 2), ("bx2", 2), ("RX010", 10)])
def test_barcodes_parse_case_and_padding_insensitively(raw, number):
    assert parse_barcode(raw).number == number


@pytest.mark.parametrize("raw", ["", "BX", "002", "QX002", "BX-002", "BX2A", "B X2"])
def test_malformed_barcodes_are_rejected(raw):
    with pytest.raises(ValueError, match="not a barcode"):
        parse_barcode(raw)


# --- akoya_id ---------------------------------------------------------------


def test_na_means_custom_conjugation_not_a_missing_field():
    assert is_custom_conjugation(NO_AKOYA_ID)
    assert is_custom_conjugation("na")
    assert not is_custom_conjugation("AKY-BX001")


def test_an_empty_akoya_id_is_rejected_but_na_is_not():
    common = {"fluorophores": [_fluorophore()], "known_markers": KNOWN}
    blank = _problems(
        antibodies=[_antibody(akoya_id="")], barcodes=[_barcode()], **common)
    assert any("empty akoya_id" in p for p in blank)

    assert not _problems(
        antibodies=[_antibody(akoya_id=NO_AKOYA_ID)], barcodes=[_barcode()], **common)


# --- what an import must refuse ---------------------------------------------


def test_a_malformed_barcode_fails_the_import():
    problems = _problems(
        antibodies=[_antibody(bx_barcode="B0001")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("not a barcode" in p for p in problems)


def test_two_unreadable_reporter_rows_are_reported_apart():
    """Each unreadable row is reported with its own location."""
    problems = _problems(
        antibodies=[_antibody()],
        barcodes=[_barcode(), _barcode(rx_barcode=""), _barcode(rx_barcode="")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    malformed = [p for p in problems if "not a barcode" in p]
    assert len(malformed) == 2
    assert len(set(malformed)) == 2, f"the two broken rows read the same: {malformed}"


def test_an_antibody_targeting_an_unknown_marker_fails_the_import():
    problems = _problems(
        antibodies=[_antibody(canonical_name="CD9999")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("unknown marker" in p for p in problems)


def test_duplicate_catalogue_numbers_fail_the_import():
    problems = _problems(
        antibodies=[_antibody(), _antibody(bx_barcode="BX002")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("duplicate catalogue_number" in p for p in problems)


def test_duplicate_barcodes_fail_the_import_across_spellings():
    problems = _problems(
        antibodies=[_antibody(), _antibody(catalogue_number="4350004", bx_barcode="bx1")],
        barcodes=[_barcode(), _barcode(rx_barcode="RX0001")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("duplicate bx_barcode" in p for p in problems)
    assert any("duplicate rx_barcode" in p for p in problems)


def test_a_reporter_naming_an_unknown_dye_fails_the_import():
    problems = _problems(
        antibodies=[_antibody()],
        barcodes=[_barcode(fluorophore="ALEXA_FLUOR_999")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("unknown fluorophore" in p for p in problems)


def test_a_reporter_barcode_in_the_antibody_namespace_fails_the_import():
    problems = _problems(
        antibodies=[_antibody(bx_barcode="RX001")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("expected BX" in p for p in problems)


def test_a_missing_column_is_reported_not_raised():
    short = {k: v for k, v in _antibody().items() if k != "canonical_name"}
    problems = _problems(
        antibodies=[short],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("missing column 'canonical_name'" in p for p in problems)


def test_a_missing_column_does_not_stop_the_other_problems_being_reported():
    short = {k: v for k, v in _antibody(bx_barcode="nope").items() if k != "vendor"}
    problems = _problems(
        antibodies=[short],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("missing column 'vendor'" in p for p in problems)
    assert any("not a barcode" in p for p in problems)


def test_an_antibody_with_no_catalogue_number_fails_the_import():
    problems = _problems(
        antibodies=[_antibody(catalogue_number="")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("empty catalogue_number" in p for p in problems)


def test_a_fluorophore_with_no_name_fails_the_import_naming_its_own_file():
    problems = _problems(
        antibodies=[_antibody()],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore(canonical_name=""), _fluorophore()],
        known_markers=KNOWN,
    )
    assert any("fluorophores.csv" in p and "empty canonical_name" in p for p in problems)
    assert not any("unknown fluorophore" in p for p in problems)


# --- the akoya set ----------------------------------------------------------


def test_an_akoya_set_pairs_the_antibody_with_its_own_reporter():
    assert not _problems(
        antibodies=[_antibody(bx_barcode="BX002", akoya_id="AKY-002")],
        barcodes=[_barcode(rx_barcode="RX002", akoya_id="AKY-002")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )


def test_an_akoya_set_whose_two_sides_disagree_fails_the_import():
    problems = _problems(
        antibodies=[_antibody(bx_barcode="BX002", akoya_id="AKY-002")],
        barcodes=[_barcode(rx_barcode="RX003", akoya_id="AKY-002")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("names one set but covers mismatched barcodes" in p for p in problems)


def test_an_antibody_and_its_own_reporter_must_carry_one_id():
    problems = _problems(
        antibodies=[_antibody(bx_barcode="BX002", akoya_id="AKY-002")],
        barcodes=[_barcode(rx_barcode="RX002", akoya_id="AKY-009")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("one set carries one id" in p for p in problems)


def test_an_unreadable_reporter_barcode_does_not_break_the_set_check():
    """A typo is reported once, as a barcode problem."""
    problems = _problems(
        antibodies=[_antibody(bx_barcode="BX002", akoya_id="AKY-002")],
        barcodes=[_barcode(rx_barcode="nope", akoya_id="AKY-009")],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert not any("one set carries one id" in p for p in problems)
    assert any("is not a barcode" in p for p in problems)


def test_two_antibodies_in_one_akoya_set_must_still_agree():
    problems = _problems(
        antibodies=[_antibody(),
                    _antibody(catalogue_number="4350004", bx_barcode="BX009")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )
    assert any("names one set but covers mismatched barcodes" in p for p in problems)


def test_na_is_exempt_from_the_set_check():
    assert not _problems(
        antibodies=[_antibody(bx_barcode="BX002", akoya_id=NO_AKOYA_ID)],
        barcodes=[_barcode(rx_barcode="RX003", akoya_id=NO_AKOYA_ID)],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )


def test_an_akoya_id_stocked_on_one_side_only_is_not_an_error():
    assert not _problems(
        antibodies=[_antibody(bx_barcode="BX007", akoya_id="AKY-007")],
        barcodes=[_barcode()],
        fluorophores=[_fluorophore()],
        known_markers=KNOWN,
    )


def test_the_seeded_inventory_has_no_reagent_side_problem():
    """Marker names are checked in clinicalomx_markers, so any name passes here."""
    antibody_rows = antibodies()
    named = {row["canonical_name"] for row in antibody_rows}
    assert not _problems(antibodies=antibody_rows, barcodes=barcodes(),
                         fluorophores=fluorophores(), known_markers=named)
