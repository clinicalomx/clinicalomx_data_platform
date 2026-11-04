"""The vocabulary loader, on a synthetic ATHENA download built per test. Concept ids are in
OMOP's local range, above 2,000,000,000; no real terminology is used."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clinicalomx_omop import athena
from clinicalomx_omop.cdm import CURRENT

_CONCEPT_HEADER = ("concept_id", "concept_name", "domain_id", "vocabulary_id",
                   "concept_class_id", "standard_concept", "concept_code", "valid_start_date",
                   "valid_end_date", "invalid_reason")
GENDER, CONDITION = 2_000_000_001, 2_000_000_002


def _tsv(path: Path, header, rows) -> None:
    path.write_text("\n".join("\t".join(map(str, r)) for r in (header, *rows)) + "\n")


def _download(root: Path, *, vocabularies=("SNOMED", "None")) -> Path:
    _tsv(root / "VOCABULARY.csv",
         ("vocabulary_id", "vocabulary_name", "vocabulary_reference", "vocabulary_version",
          "vocabulary_concept_id"),
         [(v, f"synthetic {v}", "", f"{v} synthetic-1", 0) for v in vocabularies])
    _tsv(root / "CONCEPT.csv", _CONCEPT_HEADER, [
        (GENDER, "synthetic gender a", "Gender", "SNOMED", "Clinical Finding", "S", "S-1",
         "19700101", "20991231", ""),
        (CONDITION, 'synthetic "quoted" condition', "Condition", "SNOMED",
         "Clinical Finding", "S", "S-2", "19700101", "20991231", ""),
    ])
    return root


def _refusal(**kwargs) -> str:
    with pytest.raises(athena.VocabularyUnavailable) as caught:
        athena.open_athena(**kwargs)
    return str(caught.value)


def test_an_unconfigured_vocabulary_tells_a_stranger_exactly_what_to_do(monkeypatch):
    """The error names every step needed to set up a download."""
    monkeypatch.delenv(athena.ENV, raising=False)
    message = _refusal()
    for needed in (athena.ENV, "is not set", "https://athena.ohdsi.org", "SNOMED CT licence",
                   "National Release Centre", "https://www.healthterminologies.gov.au",
                   "https://mlds.ihtsdotools.org", "CONCEPT.csv", "VOCABULARY.csv",
                   "never commit"):
        assert needed in message, f"the refusal no longer says {needed!r}"


def test_a_configured_directory_that_does_not_exist_is_named(monkeypatch, tmp_path):
    monkeypatch.setenv(athena.ENV, str(tmp_path / "nowhere"))
    message = _refusal()
    assert f"{athena.ENV}={tmp_path / 'nowhere'} does not exist" in message
    assert "https://athena.ohdsi.org" in message


def test_a_directory_that_is_not_a_download_names_the_missing_file(tmp_path):
    (tmp_path / "VOCABULARY.csv").write_text("")
    message = _refusal(path=tmp_path)
    assert "has no CONCEPT.csv" in message


def test_a_download_without_snomed_says_so(tmp_path):
    message = _refusal(path=_download(tmp_path, vocabularies=("None", "LOINC")))
    assert "does not include SNOMED" in message


def test_a_download_reports_its_snomed_release(tmp_path):
    assert athena.open_athena(_download(tmp_path)).snomed_version == "SNOMED synthetic-1"


def test_the_environment_variable_is_where_a_download_is_found(monkeypatch, tmp_path):
    monkeypatch.setenv(athena.ENV, str(_download(tmp_path)))
    assert athena.open_athena().root == tmp_path


def test_concepts_are_looked_up_by_identifier_and_absent_ones_are_absent(tmp_path):
    found = athena.open_athena(_download(tmp_path)).concepts([GENDER, CONDITION, 2_000_000_999])
    assert set(found) == {GENDER, CONDITION}
    assert found[CONDITION].concept_name == 'synthetic "quoted" condition'


def test_concept_fields_are_checked_against_the_domain_the_cdm_names(tmp_path):
    vocabulary = athena.open_athena(_download(tmp_path))
    person = CURRENT.table("person")
    good = {"person_id": 1, "gender_concept_id": GENDER, "race_concept_id": 0,
            "ethnicity_concept_id": 0}
    assert athena.check_concepts(vocabulary, person, [good]) == []

    findings = athena.check_concepts(vocabulary, person, [
        {**good, "gender_concept_id": CONDITION},
        {**good, "race_concept_id": 2_000_000_999},
    ])
    assert findings == [
        f"person row 0: gender_concept_id={CONDITION} is a Condition concept; "
        "the CDM requires Gender",
        f"person row 1: race_concept_id=2000000999 is not in the vocabulary at {tmp_path}",
    ]


def test_no_vocabulary_file_is_anywhere_in_this_package():
    """No ATHENA file is in the tree: that would redistribute SNOMED CT."""
    root = Path(__file__).resolve().parents[1]
    bundle = {"CONCEPT", "CONCEPT_RELATIONSHIP", "CONCEPT_ANCESTOR", "CONCEPT_SYNONYM",
              "VOCABULARY", "SOURCE_TO_CONCEPT_MAP", "DRUG_STRENGTH", "CONCEPT_CLASS",
              "RELATIONSHIP", "DOMAIN"}
    found = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if not d.startswith(".")]  # .venv, .pytest_cache
        found += [Path(directory, f) for f in files if Path(f).stem.upper() in bundle]
    assert found == []
