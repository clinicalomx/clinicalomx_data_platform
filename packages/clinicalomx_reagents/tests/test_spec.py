"""The column spec is the one statement of what a reagent CSV holds."""
from __future__ import annotations

import csv

import pytest

from clinicalomx_reagents import docs
from clinicalomx_reagents.rows import DATA, antibodies
from clinicalomx_reagents.spec import (
    NO_AKOYA_ID,
    REAGENT_FILES,
    Column,
    SeedFile,
    column_problems,
)


def test_the_readme_tables_are_generated_from_the_spec():
    assert docs.block(docs.README, docs.BEGIN) == docs.reagent_tables(), (
        "data/README.md is stale: run `uv run python -m clinicalomx_reagents.docs`"
    )


def test_every_file_names_exactly_one_key():
    for spec in REAGENT_FILES.values():
        assert sum(c.key for c in spec.columns) == 1, spec.filename


def test_a_blank_akoya_id_is_told_what_to_write():
    rows = [{c.name: "x" for c in REAGENT_FILES["barcodes.csv"].columns} | {"akoya_id": ""}]
    problems = column_problems(REAGENT_FILES["barcodes.csv"], rows)
    assert any(f"write {NO_AKOYA_ID}" in p for p in problems)


def _header(filename: str) -> list[str]:
    with (DATA / filename).open(newline="") as fh:
        body = (line for line in fh if not line.lstrip().startswith("#"))
        return next(csv.reader(body))


def test_every_reagent_file_has_a_spec():
    assert {p.name for p in DATA.glob("*.csv")} == set(REAGENT_FILES)


@pytest.mark.parametrize("filename", sorted(REAGENT_FILES))
def test_the_spec_states_the_columns_the_file_actually_has(filename):
    """In file order."""
    assert _header(filename) == [c.name for c in REAGENT_FILES[filename].columns]


def test_the_reader_raises_on_a_file_that_does_not_match_its_spec(tmp_path, monkeypatch):
    """A seed file that does not match its spec raises."""
    from clinicalomx_reagents import rows

    (tmp_path / "antibodies.csv").write_text("catalogue_number\n4350003\n")
    monkeypatch.setattr(rows, "DATA", tmp_path)
    with pytest.raises(ValueError, match="missing column 'bx_barcode'"):
        antibodies()


# --- what the spec catches --------------------------------------------------


_SPEC = SeedFile(
    "example.csv",
    "An illustrative file",
    (
        Column("name", "The key", key=True, blank_ok=False),
        Column("kind", "Controlled", blank_ok=False, one_of=("a", "b")),
        Column("note", "Free text"),
    ),
)


def test_a_missing_column_is_reported_by_name():
    problems = column_problems(_SPEC, [{"name": "x", "kind": "a"}])
    assert problems == ["example.csv: missing column 'note'"]


def test_a_blank_in_a_required_column_is_reported_against_the_row_key():
    problems = column_problems(_SPEC, [{"name": "x", "kind": "", "note": ""}])
    assert problems == ["example.csv: x has an empty kind"]


def test_a_row_with_no_key_is_reported_by_position():
    """A row with no key is reported by position."""
    problems = column_problems(_SPEC, [{"name": "", "kind": "a", "note": ""}])
    assert problems == ["example.csv: row 1 has an empty name"]


def test_a_value_outside_the_controlled_set_is_reported():
    problems = column_problems(_SPEC, [{"name": "x", "kind": "c", "note": ""}])
    assert problems == ["example.csv: x has kind 'c' not in (a, b)"]


def test_a_blank_optional_column_is_not_a_problem():
    assert column_problems(_SPEC, [{"name": "x", "kind": "a", "note": ""}]) == []
