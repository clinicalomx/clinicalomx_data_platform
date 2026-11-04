"""Vendor sheets written to disk and read through `propose.columns.read_sheet`."""
from __future__ import annotations

import csv

import pytest

from clinicalomx_markers.domain.reagent_mapping import MappingClaim, ReagentMappingPredicate
from clinicalomx_markers.propose.columns import (
    DESTINATION,
    SIGNATURES,
    map_columns,
    read_sheet,
    request_for,
)
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.schema import Attention, Candidate
from clinicalomx_markers.sources.sheet import columns_of

P = ReagentMappingPredicate
SIGNATURES_BY = {s.predicate for s in SIGNATURES}

HEADER = ["Catalog Number", "Reporter Barcode", "Antibody Barcode", "Dye", "Akoya ID"]


def _rows(n: int) -> list[list[str]]:
    dyes = ["ATTO550", "Alexa Fluor 647", "AF750"]
    return [[f"CAT-{i:05d}", f"RX{i % 60 + 1:03d}", f"BX{i % 60 + 1:03d}", dyes[i % 3],
             f"AKY-{i % 60 + 1:03d}"] for i in range(n)]


def _write(tmp_path, header, rows, name="akoya-panel.csv"):
    path = tmp_path / name
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerows([header, *rows])
    return path


def _mapping(result) -> dict[str, str]:
    return {c.predicate: c.value for c in result.claims}


def test_known_headers_yield_one_claim_per_field_with_checkable_evidence(tmp_path):
    result = read_sheet(_write(tmp_path, HEADER, _rows(28)))

    assert _mapping(result) == {
        P.CATALOGUE_NUMBER: "akoya-panel!A1", P.RX_BARCODE: "akoya-panel!B1",
        P.BX_BARCODE: "akoya-panel!C1", P.FLUOROPHORE: "akoya-panel!D1",
        P.AKOYA_ID: "akoya-panel!E1",
    }
    assert all(isinstance(c, MappingClaim) for c in result.claims)
    assert {c.subject for c in result.claims} == {"akoya-panel.csv"}
    by = {c.predicate: c for c in result.claims}
    assert by[P.RX_BARCODE].evidence == "header 'Reporter Barcode'; 28/28 values match RX[0-9]+"
    assert by[P.RX_BARCODE].source_id == "akoya-panel!B1"
    assert by[P.FLUOROPHORE].evidence == "header 'Dye'; 28/28 values match a known dye"
    assert by[P.CATALOGUE_NUMBER].evidence == (
        "header 'Catalog Number'; no value test for catalogue_number")
    assert result.unplaced == ()


def test_each_claim_stores_the_confidence_its_proposal_gave(tmp_path):
    """Each claim stores its proposal's confidence."""
    result = read_sheet(_write(tmp_path, HEADER, _rows(28)))
    proposed = {column.address: proposal.confidence for column, proposal in result.proposals}
    assert result.claims
    assert all(c.confidence == proposed[c.value] for c in result.claims)


def test_an_ambiguous_column_ranks_every_destination_rather_than_picking_one(tmp_path):
    rows = [["RX001"], ["BX002"], ["RX003"], ["BX004"]]
    result = read_sheet(_write(tmp_path, ["Barcode"], rows))

    [(_, proposal)] = result.proposals
    destinations = [proposal.value, *(a.value for a in proposal.alternatives)]
    assert set(destinations) == {P.RX_BARCODE, P.BX_BARCODE}
    assert proposal.margin == 0
    assert proposal.triage.label is not Attention.SKIM


def test_a_column_with_no_value_test_and_a_shared_header_is_ambiguous_too(tmp_path):
    result = read_sheet(_write(tmp_path, ["Product ID"], [["4450012"], ["4450013"]]))

    [(_, proposal)] = result.proposals
    assert {proposal.value, *(a.value for a in proposal.alternatives)} == {
        P.AKOYA_ID, P.CATALOGUE_NUMBER}
    assert proposal.triage.label is Attention.SCRUTINISE


def test_a_column_the_reader_cannot_place_is_reported_not_dropped(tmp_path):
    header = [*HEADER, "Received", "Notes", ""]
    rows = [[*r, "2026-03-01", "keep cold", "orphan value"] for r in _rows(5)]
    result = read_sheet(_write(tmp_path, header, rows))

    assert len(result.claims) == len(HEADER)
    reported = {u.column.address: u for u in result.unplaced}
    assert set(reported) == {"akoya-panel!F1", "akoya-panel!G1", "akoya-panel!H1"}
    assert reported["akoya-panel!F1"].proposal.refused
    assert "names no reagent field" in reported["akoya-panel!G1"].reason
    assert "no header" in reported["akoya-panel!H1"].reason
    # Every column of the sheet is either a claim or a report.
    assert len(result.claims) + len(result.unplaced) == len(header)


def test_deterministic_cleanup_produces_no_claims(tmp_path):
    clean = read_sheet(_write(tmp_path, HEADER, _rows(12), "clean.csv"), subject="s")
    messy_rows = [[f"  {c.lower()}  " for c in r] for r in _rows(12)]
    messy_rows.insert(3, ["", "", "", "", ""])
    messy = read_sheet(_write(tmp_path, [f" {h} " for h in HEADER], messy_rows, "messy.csv"),
                       subject="s")

    # Trimming, case and a blank row change nothing.
    assert [(c.predicate, c.value.split("!")[1]) for c in messy.claims] == [
        (c.predicate, c.value.split("!")[1]) for c in clean.claims]
    assert len(messy.claims) == len(clean.claims) == len(HEADER)
    rx = next(c for c in messy.claims if c.predicate == P.RX_BARCODE)
    assert rx.evidence == "header ' Reporter Barcode '; 12/12 values match RX[0-9]+"


@pytest.mark.parametrize("n", [1, 50, 20_000])
def test_row_count_does_not_change_claim_count(tmp_path, n):
    result = read_sheet(_write(tmp_path, HEADER, _rows(n)))

    assert len(result.claims) == len(HEADER)
    # Every field but the target and the clone.
    assert set(_mapping(result)) == SIGNATURES_BY - {P.TARGET_LABEL, P.CLONE}


def test_every_reagent_field_has_a_signature():
    assert SIGNATURES_BY == set(ReagentMappingPredicate)


def test_a_person_places_a_column_through_the_same_path():
    [column] = columns_of([["Barcode"], ["RX001"], ["BX002"]], sheet="Sheet1")
    person = HumanProposer(by="rafael")
    person.answer(request_for(column), P.RX_BARCODE, confidence=0.9, predicate=DESTINATION,
                  alternatives=(Candidate(P.BX_BARCODE, 0.1),))

    result = map_columns("panel.xlsx", [column], proposer=person, source="human")

    [claim] = result.claims
    assert (claim.predicate, claim.value, claim.source) == (P.RX_BARCODE, "Sheet1!A1", "human")
    assert claim.evidence == "header 'Barcode'; 1/2 values match RX[0-9]+"


def test_two_columns_sharing_a_header_are_two_claims_the_reviewer_is_told_about(tmp_path):
    rows = [["RX001", "BX001"], ["RX002", "BX002"], ["RX003", "BX003"]]
    result = read_sheet(_write(tmp_path, ["Barcode", "Barcode"], rows))

    assert _mapping(result) == {P.RX_BARCODE: "akoya-panel!A1", P.BX_BARCODE: "akoya-panel!B1"}
    assert {c.evidence for c in result.claims} == {
        "header 'Barcode'; 3/3 values match RX[0-9]+",
        "header 'Barcode'; 3/3 values match BX[0-9]+"}
    by = {column.address: proposal for column, proposal in result.proposals}
    for here, other in (("akoya-panel!A1", "akoya-panel!B1"),
                        ("akoya-panel!B1", "akoya-panel!A1")):
        assert by[here].triage.label is not Attention.SKIM
        assert f"header 'Barcode' is shared with {other}" in by[here].triage.reason
        assert by[here].alternatives


def test_a_person_answer_for_one_of_two_same_headed_columns_stays_on_that_column():
    first, second = columns_of([["Barcode", "Barcode"], ["RX001", "BX001"]], sheet="Sheet1")
    person = HumanProposer(by="rafael")
    person.answer(request_for(first), P.RX_BARCODE, confidence=0.95, predicate=DESTINATION)

    result = map_columns("panel.xlsx", [first, second], proposer=person, source="human")

    assert _mapping(result) == {P.RX_BARCODE: "Sheet1!A1"}
    [left] = result.unplaced
    assert left.column.address == "Sheet1!B1"
    [(_, placed)] = [(c, p) for c, p in result.proposals if c.address == "Sheet1!A1"]
    assert placed.triage.label is Attention.REVIEW
    assert "shared with Sheet1!B1" in placed.triage.reason


@pytest.mark.parametrize("predicate, value, says", [
    (None, P.RX_BARCODE, "not 'destination'"),
    (DESTINATION, "RX barcode", "is not a reagent field"),
])
def test_an_answer_naming_no_reagent_field_is_reported_and_the_sheet_still_reads(
        predicate, value, says):
    bad, good = columns_of([["Barcode", "Dye"], ["RX001", "ATTO550"]], sheet="Sheet1")
    person = HumanProposer(by="rafael")
    extra = {} if predicate is None else {"predicate": predicate}
    person.answer(request_for(bad), value, confidence=0.9, **extra)
    person.answer(request_for(good), P.FLUOROPHORE, confidence=0.9, predicate=DESTINATION)

    result = map_columns("panel.xlsx", [bad, good], proposer=person, source="human")

    assert _mapping(result) == {P.FLUOROPHORE: "Sheet1!B1"}
    [left] = result.unplaced
    assert left.column.address == "Sheet1!A1"
    assert says in left.reason


def test_an_xlsx_reads_the_named_sheet_with_its_own_addresses(tmp_path):
    import openpyxl

    book = openpyxl.Workbook()
    book.active.title = "Cover"
    ws = book.create_sheet("Inventory")
    ws.append(HEADER)
    for row in _rows(6):
        ws.append(row)
    ws.append([1234.0, None, None, None, None])
    path = tmp_path / "akoya-panel-2026-03.xlsx"
    book.save(path)

    result = read_sheet(path, sheet="Inventory")

    assert len(result.claims) == len(HEADER)
    rx = next(c for c in result.claims if c.predicate == P.RX_BARCODE)
    assert (rx.subject, rx.source_id) == ("akoya-panel-2026-03.xlsx", "Inventory!B1")
    assert rx.evidence == "header 'Reporter Barcode'; 6/6 values match RX[0-9]+"
