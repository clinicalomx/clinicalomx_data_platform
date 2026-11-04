"""A core ROI reaches its patient's OMOP row, and its features meet that patient's data.

One narrative on synthetic data: declare, write persons, claim, accept, project, build X/Y,
then correct by appending. Concept ids are from OMOP's local range, above 2,000,000,000.
"""

from __future__ import annotations

import datetime

import lamindb as ln
import pytest
from clinicalomx.names import FEATURES, TYPES
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import LedgerClaimStore

from clinicalomx_omop import declare, link
from clinicalomx_omop.cdm import CURRENT

HUMAN = {"actor": "synthetic-curator", "actor_kind": "human"}
ACCEPT = Judgement(by="synthetic-curator", at="2026-09-28", reason="map row matches")
REJECT = Judgement(by="synthetic-curator", at="2026-09-29", kind="one_off",
                   reason="map was for the previous cut")
SYNTHETIC = 2_000_000_000


def _person(person_id: int, year: int) -> dict:
    return {"person_id": person_id, "gender_concept_id": SYNTHETIC + 1, "year_of_birth": year,
            "race_concept_id": 0, "ethnicity_concept_id": 0}


def _measurement(measurement_id: int, person_id: int, value: float) -> dict:
    return {"measurement_id": measurement_id, "person_id": person_id,
            "measurement_concept_id": SYNTHETIC + 2, "measurement_date": datetime.date(2020, 1, 1),
            "measurement_type_concept_id": SYNTHETIC + 3, "value_as_number": value}


@pytest.fixture(scope="module")
def world():
    """The CDM declared, two persons, and a TMA with two cores carrying one X feature each."""
    types = declare.ensure()
    for person_id, year in ((1, 1950), (2, 1970)):
        link.add_row(CURRENT, "person", _person(person_id, year))
    for mid, pid, value in ((10, 1, 4.0), (11, 1, 6.0), (12, 2, 1.5)):
        link.add_row(CURRENT, "measurement", _measurement(mid, pid, value))

    density = ln.Feature(name="synthetic_cd8_density", dtype=float).save()
    tma = ln.Record(name="s:TMA0", type=ln.Record.get(name=TYPES.TMA, is_type=True)).save()
    core_type = ln.Record.get(name=TYPES.CORE, is_type=True)
    cores = {}
    for name, value in (("s:TMA0:R0C0", 0.25), ("s:TMA0:R0C1", 0.75)):
        cores[name] = ln.Record(name=name, type=core_type, features={density.name: value}).save()
    store = LedgerClaimStore("main", link.PATIENT_DOMAIN, **HUMAN)
    return {"types": types, "tma": tma, "cores": cores, "store": store}


def test_every_modelled_table_is_a_record_type_bound_to_its_schema(world):
    for table in CURRENT.tables:
        record_type = world["types"][table.name]
        assert record_type.name == CURRENT.type_name(table.name)
        assert record_type.schema.minimal_set and record_type.schema.maximal_set
        assert record_type.schema.n_members == len(table.fields)
    assert world["types"]["person"].type.name == TYPES.PATIENT
    assert world["types"]["specimen"].type.name == CURRENT.name


def test_declaring_again_changes_nothing(world):
    before = ln.Feature.filter().count(), ln.Schema.filter().count(), ln.Record.filter().count()
    assert declare.ensure() == world["types"]
    after = ln.Feature.filter().count(), ln.Schema.filter().count(), ln.Record.filter().count()
    assert before == after


def test_a_row_missing_a_field_the_cdm_requires_is_refused(world):
    row = _person(3, 1980)
    del row["year_of_birth"]
    with pytest.raises(ln.errors.ValidationError):
        link.add_row(CURRENT, "person", row)
    assert not ln.Record.filter(type=world["types"]["person"], name="3").exists()


def test_a_field_the_cdm_does_not_define_is_refused(world):
    with pytest.raises(ln.errors.ValidationError):
        link.add_row(CURRENT, "person", {**_person(4, 1980), FEATURES.PATIENT_ID: "x"})
    assert not ln.Record.filter(type=world["types"]["person"], name="4").exists()


def test_a_death_row_is_written_under_its_person_id(world):
    link.add_row(CURRENT, "death", {"person_id": 2, "death_date": datetime.date(2024, 5, 1)})
    death = ln.Record.get(type=world["types"]["death"], name="2")
    assert death.features.get_values()["death_date"] == datetime.date(2024, 5, 1)


def test_a_person_already_written_is_refused_and_nothing_new_is_written(world):
    before = ln.Record.filter(type=world["types"]["person"]).count()
    with pytest.raises(ValueError, match="person_id=1"):
        link.add_row(CURRENT, "person", _person(1, 1990))
    assert ln.Record.filter(type=world["types"]["person"]).count() == before


def test_a_measurement_for_an_unknown_person_is_refused(world):
    with pytest.raises(LookupError, match="person_id=98"):
        link.add_row(CURRENT, "measurement", _measurement(13, 98, 2.0))
    assert not ln.Record.filter(type=world["types"]["measurement"], name="13").exists()


def test_a_claim_naming_no_person_is_refused_before_anything_is_written(world):
    with pytest.raises(LookupError, match="person_id=99"):
        link.propose(world["store"], roi="s:TMA0:R0C0", person_id=99,
                     source=link.PatientSource.SLIDE_MAP, source_id="synthetic-map-1",
                     evidence="row 0 col 0")
    assert world["store"].held("s:TMA0:R0C0") == []


def test_an_unaccepted_claim_projects_nothing(world):
    store, cores = world["store"], world["cores"]
    world["claims"] = {
        name: link.propose(store, roi=name, person_id=pid,
                           source=link.PatientSource.SLIDE_MAP, source_id="synthetic-map-1",
                           evidence=f"cell for {name}")
        for name, pid in (("s:TMA0:R0C0", 1), ("s:TMA0:R0C1", 2))
    }
    assert link.project(store, cores["s:TMA0:R0C0"]) is None
    assert FEATURES.PATIENT not in cores["s:TMA0:R0C0"].features.get_values()


def test_an_accepted_claim_links_the_core_to_its_person_row(world):
    store, cores = world["store"], world["cores"]
    for claim in world["claims"].values():
        store.decide(claim, outcome="accepted", judgement=ACCEPT)
    for name, pid in (("s:TMA0:R0C0", 1), ("s:TMA0:R0C1", 2)):
        person = link.project(store, cores[name])
        assert person.type == world["types"]["person"]
        assert person.features.get_values()["person_id"] == pid
        assert cores[name].features.get_values()[FEATURES.PATIENT] == str(pid)


def test_roi_features_are_x_and_the_patient_s_omop_values_are_y(world):
    frame = link.training_frame(
        world["cores"].values(), x=["synthetic_cd8_density"],
        y=[("person", "year_of_birth"), ("measurement", "value_as_number")])
    rows = frame.set_index("roi").to_dict("index")
    assert rows["s:TMA0:R0C0"]["synthetic_cd8_density"] == 0.25
    assert rows["s:TMA0:R0C0"]["person.year_of_birth"] == 1950
    assert sorted(rows["s:TMA0:R0C0"]["measurement.value_as_number"]) == [4.0, 6.0]
    assert rows["s:TMA0:R0C1"]["person.year_of_birth"] == 1970
    assert rows["s:TMA0:R0C1"]["measurement.value_as_number"] == [1.5]


def test_a_container_is_refused_as_a_training_row(world):
    with pytest.raises(link.NotAnAnalysisUnit, match="s:TMA0 \\(no patient\\)"):
        link.training_frame([world["tma"], *world["cores"].values()], x=[], y=[])


def test_a_correction_is_appended_and_two_accepted_patients_are_refused(world):
    """A correction accepted beside the old claim gives two patients and is refused until the
    old one is rejected."""
    store, core = world["store"], world["cores"]["s:TMA0:R0C1"]
    fixed = link.propose(store, roi=core.name, person_id=1, source=link.PatientSource.HUMAN,
                         source_id="synthetic-curator", evidence="re-read the block label")
    store.decide(fixed, outcome="accepted", judgement=ACCEPT)
    with pytest.raises(link.NotAnAnalysisUnit, match="persons 1, 2"):
        link.project(store, core)

    store.decide(world["claims"][core.name], outcome="rejected", judgement=REJECT)
    assert link.project(store, core).features.get_values()["person_id"] == 1
    assert len(store.held(core.name, history=True)) == 2, "nothing was edited away"
