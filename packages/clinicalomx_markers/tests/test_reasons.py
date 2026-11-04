"""Typed reasons and column profiles, against a committed fixture sheet. Expected numbers are
counted here with the standard library."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import pytest

from clinicalomx_markers.domain.reagent_mapping import ReagentMappingPredicate as P
from clinicalomx_markers.propose.columns import (
    ColumnProposer,
    column_profile,
    map_columns,
    request_for,
    source_profile,
)
from clinicalomx_markers.propose.schema import Reason, ReasonKind, confidence_from
from clinicalomx_markers.sources.sheet import TOP_VALUES, columns_of, read_rows

SHEET = Path(__file__).parent / "fixtures" / "sheets" / "io_panel.csv"


def _columns():
    name, rows = read_rows(SHEET)
    return columns_of(rows, sheet=name)


def _proposal(header: str):
    column = next(c for c in _columns() if c.header == header)
    (proposal,) = ColumnProposer().propose(request_for(column))
    return column, proposal


def _file_column(header: str) -> list[str]:
    with SHEET.open(newline="") as fh:
        rows = list(csv.reader(fh))
    at = rows[0].index(header)
    return [r[at].strip() for r in rows[1:]]


def test_a_header_only_proposal_says_what_was_not_checked_and_what_held_it_back():
    _, proposal = _proposal("Catalog Number")
    kinds = {r.kind: r for r in proposal.reasons}
    assert set(kinds) == {ReasonKind.SEMANTIC, ReasonKind.NOT_APPLICABLE, ReasonKind.HELD_BACK}
    assert kinds[ReasonKind.SEMANTIC].outcome == "passed"
    assert kinds[ReasonKind.NOT_APPLICABLE].weight is None
    assert kinds[ReasonKind.NOT_APPLICABLE].check == "value test"
    assert kinds[ReasonKind.HELD_BACK].weight < 0
    # The confidence is computed from the reasons.
    assert proposal.confidence == confidence_from(proposal.reasons) == 0.8


def test_a_value_test_is_a_counted_pattern_or_vocabulary_reason():
    _, rx = _proposal("Reporter Barcode")
    (pattern,) = [r for r in rx.reasons if r.kind is ReasonKind.PATTERN]
    assert pattern.weight == 1.0 and "10/10" in pattern.evidence

    _, dye = _proposal("Dye")
    (hit,) = [r for r in dye.reasons if r.kind is ReasonKind.VOCABULARY]
    known = [v for v in _file_column("Dye") if v and v != "Opal 999"]
    assert f"{len(known)}/9" in hit.evidence
    assert dye.confidence == confidence_from(dye.reasons)


def test_a_check_that_ran_and_failed_is_zero_not_absent():
    # "Barcode" names both barcode fields; BX values fail rx_barcode's pattern outright.
    column = next(c for c in _columns() if c.header == "BX Barcode")
    renamed = columns_of([["Barcode"], *[[v] for v in column.values]])[0]
    (proposal,) = ColumnProposer().propose(request_for(renamed))
    rx = next(a for a in proposal.alternatives if a.value == P.RX_BARCODE)
    assert rx.reasoning.endswith("0/10 values match RX[0-9]+")


def test_a_reason_refuses_a_weight_its_kind_cannot_carry():
    with pytest.raises(ValueError, match="no weight"):
        Reason(ReasonKind.NOT_APPLICABLE, "no unit on a text slot", 0.5, check="unit check")
    with pytest.raises(ValueError, match="lowers"):
        Reason(ReasonKind.HELD_BACK, "4 blanks", 0.1)
    with pytest.raises(ValueError, match="evidence"):
        Reason(ReasonKind.SEMANTIC, " ", 1.0)
    assert Reason.from_json(Reason(ReasonKind.PRECEDENT, "Q2 sheet", 0.6).to_json()).weight == 0.6


@pytest.mark.parametrize("header", ["Reporter Barcode", "BX Barcode", "Dye", "Catalog Number",
                                    "Notes"])
def test_a_column_profile_matches_the_file(header):
    column = next(c for c in _columns() if c.header == header)
    got = column_profile(column)
    values = _file_column(header)
    filled = [v for v in values if v]
    counts = Counter(filled)
    assert (got["rows"], got["blank"], got["unique"]) == (
        len(values), len(values) - len(filled), len(counts))
    assert [(v["value"], v["count"]) for v in got["top"]] == counts.most_common(TOP_VALUES)
    assert got["header"] == header


def test_a_value_that_fails_the_slot_is_noted_and_only_that_one():
    column = next(c for c in _columns() if c.header == "BX Barcode")
    top = column_profile(column, P.BX_BARCODE)["top"]
    noted = {v["value"] for v in top if v.get("note")}
    assert noted == {"BX-104"}
    dye = column_profile(next(c for c in _columns() if c.header == "Dye"), P.FLUOROPHORE)
    assert {v["value"] for v in dye["top"] if v.get("note")} == {"Opal 999"}
    assert "note" not in dye["top"][0]


def test_the_types_are_read_off_the_cells():
    by = {c.header: column_profile(c)["type"] for c in _columns()}
    assert by["Catalog Number"] == "number" and by["Dye"] == "text"


def test_a_column_with_no_claim_is_in_the_sheet_profile_with_why():
    mapping = map_columns("io_panel.csv", _columns())
    sheet = source_profile(mapping, file="io_panel.csv", content_hash="abc")
    assert (sheet["rows"], sheet["columns"]) == (len(_file_column("Notes")), 5)
    (notes,) = sheet["unclaimed"]
    assert notes["header"] == "Notes" and "names no reagent field" in notes["reason"]
    assert {c.value for c in mapping.claims} == {"io_panel!A1", "io_panel!B1", "io_panel!C1",
                                                 "io_panel!D1"}
