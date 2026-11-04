"""Structures are GO identifiers read from curated UniProt prose.

A structure comes from a SUBCELLULAR LOCATION term through ``structures.csv``. The go_c
cross-references in `UniProtRecord.go_cc` are never read: LDHA's curated localisation is
Cytoplasm, while its go_c adds nucleus and membrane.

Fixtures are captured UniProt entries."""
from __future__ import annotations

import pytest
from clinicalomx.vocab.ontology import validate_pin
from tests.conftest import load_fixture

from clinicalomx_markers.domain import structure
from clinicalomx_markers.domain.marker import MarkerPredicate
from clinicalomx_markers.domain.structure import Mapped, Structure, unmapped_terms
from clinicalomx_markers.propose.localisation import claims_from
from clinicalomx_markers.seed_data.rows import structures
from clinicalomx_markers.seed_spec import GO_RELEASE
from clinicalomx_markers.sources.snapshot import capture
from clinicalomx_markers.sources.uniprot import ENTRY_LEVEL, parse

LDHA = "P00338"
NUCLEUS, MEMBRANE, CYTOPLASM = "GO:0005634", "GO:0016020", "GO:0005737"
PLASMA_MEMBRANE = "GO:0005886"


def _record(accession):
    return parse(load_fixture(accession))


def test_a_curated_prose_term_maps_to_its_go_identifier():
    assert structure.read(_record(LDHA)).go_ids == (CYTOPLASM,)


def test_the_mapping_says_which_term_produced_which_identifier():
    """Each identifier names the prose term that produced it."""
    reading = structure.read(_record("P01732"))     # CD8A: two isoforms, two answers
    assert reading.mapped == (
        Mapped("Isoform 1", "Cell membrane", Structure(PLASMA_MEMBRANE, "plasma membrane")),
        Mapped("Isoform 2", "Secreted", Structure("GO:0005576", "extracellular region")),
    )
    assert reading.unmapped == ()


def test_every_row_of_the_table_is_what_the_mapping_reads():
    """Every identifier comes from a row of the CSV."""
    table = structure.mapping()
    assert {term: s.go_id for term, s in table.items()} == {
        row["uniprot_location"]: row["go_id"] for row in structures()}


def test_ldha_does_not_acquire_nucleus_or_membrane():
    """LDHA's curated localisation is Cytoplasm; its go_c nucleus and membrane are ignored."""
    record = _record(LDHA)
    assert {NUCLEUS, MEMBRANE} <= set(record.go_cc), "the fixture no longer shows the hazard"

    assert structure.read(record).go_ids == (CYTOPLASM,)


def test_the_go_claim_is_read_from_the_prose_and_not_from_go_c(tmp_path):
    fetched = load_fixture(LDHA)
    snap = capture(tmp_path, source="uniprot", source_id=LDHA, retrieved_at="2026-09-24",
                   fetch=lambda: fetched)
    [go] = [c for c in claims_from(snap, "LDHA") if c.predicate == MarkerPredicate.GO_CC]
    assert go.value == CYTOPLASM
    assert go.evidence == "Cytoplasm"


def test_a_term_the_table_does_not_map_is_a_gap_and_not_a_fallback():
    record = _record(LDHA)
    reading = structure.read(record, {})
    assert reading.mapped == ()
    assert reading.unmapped == ((ENTRY_LEVEL, "Cytoplasm"),)


@pytest.mark.parametrize("accession",
                         ["O75390", "P00338", "P01732", "P05023", "P08670", "P12830", "Q9BZS1"])
def test_every_captured_term_is_mapped(accession):
    """The captured fixtures have no unmapped terms."""
    assert structure.read(_record(accession)).unmapped == ()


def test_a_term_mapped_twice_is_refused():
    row = {"uniprot_location": "Cytoplasm", "go_id": CYTOPLASM, "go_name": "cytoplasm",
           "go_release": GO_RELEASE}
    with pytest.raises(ValueError, match="mapped twice"):
        structure.mapping([row, row])


def test_a_structure_is_a_value():
    assert Structure(CYTOPLASM, "cytoplasm") == Structure(CYTOPLASM, "cytoplasm")
    with pytest.raises(ValueError, match="not a GO identifier"):
        Structure("cytoplasm")


def test_a_structure_named_at_another_release_is_refused():
    with pytest.raises(ValueError, match="pinned"):
        Structure(CYTOPLASM, "cytoplasm", release="2024-11-03")


def test_the_go_pin_is_a_dated_release_and_the_only_one_a_structure_takes():
    validate_pin("GO", GO_RELEASE)
    with pytest.raises(ValueError):
        validate_pin("GO", "latest")
    with pytest.raises(ValueError, match="pinned"):
        Structure(CYTOPLASM, "cytoplasm", release="2024-11-03")


def test_a_term_the_table_lacks_is_named_in_the_curate_time_report():
    record = _record(LDHA)
    table = {k: v for k, v in structure.mapping().items() if k != "Cytoplasm"}
    [finding] = unmapped_terms(record, "LDHA", table)
    assert finding.startswith("LDHA: 'Cytoplasm'")
    assert ENTRY_LEVEL in finding
    assert "seed_data/structures.csv" in finding
    assert unmapped_terms(record, "LDHA") == []


def test_every_mapping_row_was_checked_at_the_pinned_release():
    assert {row["go_release"] for row in structures()} == {GO_RELEASE}

