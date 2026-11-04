"""The committed CDM module is the specification, rendered, and nothing else."""

from __future__ import annotations

import csv
import datetime
import importlib.util
import tomllib
from pathlib import Path

import pytest

from clinicalomx_omop.cdm import CURRENT, Field

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("generate_cdm", ROOT / "scripts/generate_cdm.py")
generate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate)
SOURCE = tomllib.loads((ROOT / "spec/source.toml").read_text())


def _spec_rows():
    path = ROOT / "spec" / SOURCE["files"]["field_level"]["name"]
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_the_committed_module_is_a_fresh_render():
    committed = ROOT / "src/clinicalomx_omop/cdm/v5_4.py"
    assert committed.read_text() == generate.render(SOURCE), "run scripts/generate_cdm.py"


def test_every_modelled_field_is_the_spec_s_own_in_the_spec_s_order():
    for table in CURRENT.tables:
        rows = [r for r in _spec_rows() if r["cdmTableName"] == table.name]
        assert [f.name for f in table.fields] == [r["cdmFieldName"] for r in rows]
        assert [f.required for f in table.fields] == [r["isRequired"] == "TRUE" for r in rows]
        assert [f.datatype for f in table.fields] == [r["cdmDatatype"] for r in rows]


def test_the_nine_clinical_tables_are_modelled_and_no_vocabulary_table_is():
    names = {t.name for t in CURRENT.tables}
    assert names == set(generate.MODELLED)
    assert not {n for n in names if n.startswith("concept") or n == "vocabulary"}


def test_a_spec_file_that_is_not_the_pinned_one_is_refused():
    tampered = {**SOURCE, "files": {**SOURCE["files"], "field_level": {
        **SOURCE["files"]["field_level"], "sha256": "0" * 64}}}
    with pytest.raises(SystemExit, match="does not match spec/source.toml"):
        generate.render(tampered)


def test_every_generated_datatype_has_a_lamindb_dtype():
    assert {f.dtype for t in CURRENT.tables for f in t.fields} <= {
        int, float, str, datetime.date, datetime.datetime}


def test_a_datatype_nobody_mapped_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="no lamindb dtype"):
        _ = Field("x", "geometry", required=False).dtype


def test_the_release_names_carry_the_cdm_version():
    assert CURRENT.name == "OMOP_CDM_v5.4"
    assert CURRENT.type_name("person") == "OMOP_CDM_v5.4.person"
