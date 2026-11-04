"""Tests for the ROI-to-patient rule: an ROI defines at most one patient; an ROI holding several
is a container and carries none. Uses a stub Snapshot."""
from __future__ import annotations

from clinicalomx.checks import roi as checks_roi
from clinicalomx.names import FEATURES


class _Rec:
    def __init__(self, name, type_name, **values):
        self.name, self.uid, self.type_name = name, name, type_name
        self.values = values


class _Snap:
    """Enough of Snapshot for the patient checks: types, records, values, parent."""

    def __init__(self, records, parents=None):
        self._records = list(records)
        self._parents = parents or {}

    def roi_types(self):
        return list(dict.fromkeys(r.type_name for r in self._records))

    def records(self, type_name):
        return [r for r in self._records if r.type_name == type_name]

    def values(self, record):
        return record.values

    def parent(self, record):
        name = self._parents.get(record.name)
        return next((r for r in self._records if r.name == name), None)


def _core(name, **values):
    return _Rec(name, "Core", **values)


def test_a_scalar_patient_is_the_ordinary_case():
    snap = _Snap([_core("s:TMA0:R0C0", **{FEATURES.PATIENT_ID: "P1"})])
    assert checks_roi.one_patient_per_roi(snap) == []


def test_an_roi_carrying_two_patients_is_reported():
    snap = _Snap([_core("s:TMA0", **{FEATURES.PATIENT_ID: ["P1", "P2"]})])
    failures = checks_roi.one_patient_per_roi(snap)
    assert len(failures) == 1
    assert "P1" in failures[0] and "P2" in failures[0]


def test_an_roi_carrying_no_patient_is_not_a_fault():
    snap = _Snap([_core("s:TMA0"), _core("s:TMA0:R0C0", **{FEATURES.PATIENT_ID: "P1"})])
    assert checks_roi.one_patient_per_roi(snap) == []


def test_the_record_link_is_checked_as_well_as_the_string_id():
    snap = _Snap([_core("s:TMA0", **{FEATURES.PATIENT: ["P1", "P2"]})])
    assert len(checks_roi.one_patient_per_roi(snap)) == 1


def test_a_child_may_repeat_its_parent_s_patient():
    snap = _Snap(
        [_Rec("slide", "WholeSlide", **{FEATURES.PATIENT_ID: "P1"}),
         _Rec("slide:T0", "Tile", **{FEATURES.PATIENT_ID: "P1"})],
        parents={"slide:T0": "slide"},
    )
    assert checks_roi.patient_agrees_with_the_parent(snap) == []


def test_a_child_contradicting_its_parent_is_reported():
    snap = _Snap(
        [_Rec("slide", "WholeSlide", **{FEATURES.PATIENT_ID: "P1"}),
         _Rec("slide:T0", "Tile", **{FEATURES.PATIENT_ID: "P2"})],
        parents={"slide:T0": "slide"},
    )
    failures = checks_roi.patient_agrees_with_the_parent(snap)
    assert len(failures) == 1
    assert "slide:T0" in failures[0] and "P2" in failures[0] and "P1" in failures[0]


def test_a_container_parent_is_passed_over_rather_than_reported():
    snap = _Snap(
        [_Rec("s:TMA0", "TMA"),
         _core("s:TMA0:R0C0", **{FEATURES.PATIENT_ID: "P1"}),
         _core("s:TMA0:R0C1", **{FEATURES.PATIENT_ID: "P2"})],
        parents={"s:TMA0:R0C0": "s:TMA0", "s:TMA0:R0C1": "s:TMA0"},
    )
    assert checks_roi.patient_agrees_with_the_parent(snap) == []


def test_a_multi_valued_cell_is_left_to_the_cardinality_check():
    snap = _Snap(
        [_Rec("s:TMA0", "TMA", **{FEATURES.PATIENT_ID: ["P1", "P2"]}),
         _core("s:TMA0:R0C0", **{FEATURES.PATIENT_ID: "P1"})],
        parents={"s:TMA0:R0C0": "s:TMA0"},
    )
    assert checks_roi.patient_agrees_with_the_parent(snap) == []


def test_both_checks_are_registered_with_the_framework():
    names = {c.name for c in checks_roi.framework_checks()}
    assert {"roi.one_patient_per_roi", "roi.patient_agrees_with_the_parent"} <= names
