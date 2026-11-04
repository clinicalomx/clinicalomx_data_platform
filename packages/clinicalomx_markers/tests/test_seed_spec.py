"""The column spec checked against each seed file's real header and the generated README.

The reagent files and the column machinery are tested in `clinicalomx_reagents`.
"""
from __future__ import annotations

import csv

import pytest
from clinicalomx_reagents.spec import column_problems

from clinicalomx_markers import seed_docs
from clinicalomx_markers.resolve import reset_cache
from clinicalomx_markers.seed_data import rows
from clinicalomx_markers.seed_data.rows import seed_rows
from clinicalomx_markers.seed_spec import SEED_FILES


def _header(filename: str) -> list[str]:
    with (rows.SEED_DATA / filename).open(newline="") as fh:
        body = (line for line in fh if not line.lstrip().startswith("#"))
        return next(csv.reader(body))


# --- the spec against the files it describes --------------------------------


def test_every_seed_file_has_a_spec():
    on_disk = {p.name for p in rows.SEED_DATA.glob("*.csv")}
    assert on_disk == set(SEED_FILES), "a seed file with no spec cannot be validated"


@pytest.mark.parametrize("filename", sorted(SEED_FILES))
def test_the_spec_states_the_columns_the_file_actually_has(filename):
    """In file order."""
    assert _header(filename) == [c.name for c in SEED_FILES[filename].columns]


@pytest.mark.parametrize("filename", sorted(SEED_FILES))
def test_every_curated_file_satisfies_its_own_spec(filename):
    assert column_problems(SEED_FILES[filename], seed_rows(filename)) == []


def test_every_spec_names_exactly_one_key():
    for spec in SEED_FILES.values():
        keys = [c.name for c in spec.columns if c.key]
        assert len(keys) == 1, f"{spec.filename} names {keys} as key"


# --- the reader enforces it -------------------------------------------------


def test_the_reader_raises_on_a_file_that_does_not_match_its_spec(tmp_path, monkeypatch):
    """A seed file that does not match its spec raises."""
    (tmp_path / "markers.csv").write_text(
        "canonical_name,full_name,state\nDAPI,,draft\n")
    monkeypatch.setattr(rows, "SEED_DATA", tmp_path)
    reset_cache()
    with pytest.raises(ValueError, match="missing column 'status'"):
        seed_rows("markers.csv")
    reset_cache()


# --- the documentation cannot drift -----------------------------------------


def test_the_readme_tables_are_generated_from_the_spec():
    assert seed_docs.block() == seed_docs.render(), (
        "seed_data/README.md is stale: run `uv run python -m clinicalomx_markers.seed_docs`"
    )
