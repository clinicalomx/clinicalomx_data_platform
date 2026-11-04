"""Each pipeline check finds its defect, and stays quiet on healthy data.

A stub Snapshot stands in for the database. The storage-backed checks are not covered here.
"""
from __future__ import annotations

import pytest

from qptiff_pipeline.checks import pipeline as checks
from qptiff_pipeline.domain import qptiff, states, vocab


class _Rec:
    def __init__(self, name, uid=None):
        self.name = name
        self.uid = uid or name


class _Snap:
    """The Snapshot accessors the pipeline predicates use."""

    def __init__(self, records=None, values=None, unit_states=None):
        self._records = records or {}
        self._values = values or {}
        self._states = unit_states or {}

    def load_values(self, type_name, features=None):
        """No-op: the stub is already loaded. A check over a non-ROI type must call it."""

    def records(self, type_name):
        return self._records.get(type_name, [])

    def values(self, record):
        return self._values.get(record.name, {})

    def states(self, type_name):
        return self._states.get(type_name, {})


TMA = "slide:TMA0"
CORE = f"{TMA}:R0C0"


# A TMA past the core gate must have registered Cores.
@pytest.mark.parametrize(
    "state",
    [states.S.CORE_APPROVED, states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING,
     states.S.BGNORM_PENDING, states.S.COMPLETE],
)
def test_a_tma_past_the_core_gate_with_no_cores_is_reported(state):
    snap = _Snap(records={states.TMA_TYPE: [_Rec(TMA)], states.CORE_TYPE: []},
                 unit_states={states.TMA_TYPE: {TMA: state}})
    (found,) = checks.approved_tmas_have_cores(snap)
    assert TMA in found and "0 Cores" in found


def test_a_tma_with_cores_is_not_reported():
    snap = _Snap(records={states.TMA_TYPE: [_Rec(TMA)], states.CORE_TYPE: [_Rec(CORE)]},
                 unit_states={states.TMA_TYPE: {TMA: states.S.CORE_APPROVED}})
    assert checks.approved_tmas_have_cores(snap) == []


def test_a_tma_before_the_core_gate_is_not_reported():
    snap = _Snap(records={states.TMA_TYPE: [_Rec(TMA)], states.CORE_TYPE: []},
                 unit_states={states.TMA_TYPE: {TMA: states.S.CORE_PENDING}})
    assert checks.approved_tmas_have_cores(snap) == []


# The runner only advances a TMA with at least one bgnormed core.
def test_bgnorm_pending_with_no_bgnormed_core_is_reported():
    snap = _Snap(records={states.TMA_TYPE: [_Rec(TMA)], states.CORE_TYPE: [_Rec(CORE)]},
                 values={CORE: {}},
                 unit_states={states.TMA_TYPE: {TMA: states.S.BGNORM_PENDING}})
    (found,) = checks.bgnorm_pending_tmas_have_results(snap)
    assert "0 bgnormed cores" in found


def test_one_bgnormed_core_is_enough():
    other = f"{TMA}:R0C1"
    snap = _Snap(
        records={states.TMA_TYPE: [_Rec(TMA)], states.CORE_TYPE: [_Rec(CORE), _Rec(other)]},
        values={CORE: {vocab.BGNORMED: "some/key.zarr"}, other: {}},
        unit_states={states.TMA_TYPE: {TMA: states.S.BGNORM_PENDING}},
    )
    assert checks.bgnorm_pending_tmas_have_results(snap) == []


# bgnormed is written after the QC table, so a zarr implies QC.
def test_a_zarr_without_qc_is_reported():
    snap = _Snap(records={states.CORE_TYPE: [_Rec(CORE)]},
                 values={CORE: {vocab.BGNORMED: "some/key.zarr"}})
    (found,) = checks.bgnormed_cores_have_qc(snap)
    assert "no QC table" in found


def test_qc_without_a_zarr_is_not_reported():
    snap = _Snap(records={states.CORE_TYPE: [_Rec(CORE)]},
                 values={CORE: {vocab.BGNORM_QC_FEATURE: "some/qc.parquet"}})
    assert checks.bgnormed_cores_have_qc(snap) == []


def test_both_present_is_not_reported():
    snap = _Snap(records={states.CORE_TYPE: [_Rec(CORE)]},
                 values={CORE: {vocab.BGNORMED: "z", vocab.BGNORM_QC_FEATURE: "q"}})
    assert checks.bgnormed_cores_have_qc(snap) == []


# A Core must be able to locate its geometry row.
def test_a_core_with_neither_identity_nor_position_is_reported():
    snap = _Snap(records={states.CORE_TYPE: [_Rec(CORE)]}, values={CORE: {}})
    (found,) = checks.cores_have_geometry_identity(snap)
    assert "neither roi_id nor parquet_row" in found


@pytest.mark.parametrize(
    "vals",
    [{"roi_id": "R0C0"}, {"parquet_row": 0}, {"roi_id": "R0C0", "parquet_row": 3}],
    ids=["roi_id", "parquet_row-fallback", "both"],
)
def test_either_identity_or_position_satisfies_it(vals):
    snap = _Snap(records={states.CORE_TYPE: [_Rec(CORE)]}, values={CORE: vals})
    assert checks.cores_have_geometry_identity(snap) == []


# QptiffChannels: provenance, and a complete index run.
CH_TYPE = qptiff.QPTIFF_CHANNELS_TYPE
QPTIFF = "projects/x/slide.qptiff"


def _channels(*specs):
    """``_Snap`` over channel Records from ``(name, index, source)`` triples."""
    records, values = [], {}
    for name, index, source in specs:
        records.append(_Rec(name))
        vals = {}
        if index is not None:
            vals[qptiff.QPTIFF_CH_INDEX] = index
        if source is not None:
            vals["source_artifact"] = source
        values[name] = vals
    return _Snap(records={CH_TYPE: records}, values=values)


def test_a_channel_record_without_a_source_is_reported():
    snap = _channels(("slide#ch00", 0, None))
    (found,) = checks.qptiff_channels_name_their_source(snap)
    assert "no source_artifact" in found


def test_channel_records_with_sources_are_not_reported():
    snap = _channels(("slide#ch00", 0, QPTIFF), ("slide#ch01", 1, QPTIFF))
    assert checks.qptiff_channels_name_their_source(snap) == []


def test_a_gap_in_the_channel_indices_is_reported():
    snap = _channels(("slide#ch00", 0, QPTIFF), ("slide#ch02", 2, QPTIFF))
    (found,) = checks.qptiff_channel_indices_are_contiguous(snap)
    assert "missing [1]" in found


def test_a_duplicated_channel_index_is_reported():
    snap = _channels(("slide#ch00", 0, QPTIFF), ("slide#ch00-dup", 0, QPTIFF))
    (found,) = checks.qptiff_channel_indices_are_contiguous(snap)
    assert "duplicated [0]" in found


def test_cancelling_faults_are_still_reported():
    snap = _channels(("a", 0, QPTIFF), ("b", 0, QPTIFF), ("c", 2, QPTIFF))
    found = checks.qptiff_channel_indices_are_contiguous(snap)
    assert found and "missing [1]" in found[0] and "duplicated [0]" in found[0]


def test_a_complete_run_is_not_reported():
    snap = _channels(*[(f"slide#ch{i:02d}", i, QPTIFF) for i in range(40)])
    assert checks.qptiff_channel_indices_are_contiguous(snap) == []


def test_channel_index_zero_is_not_mistaken_for_absent():
    snap = _channels(("slide#ch00", 0, QPTIFF))
    assert checks.qptiff_channel_indices_are_contiguous(snap) == []


def test_two_qptiffs_are_scored_independently():
    snap = _channels(("a#ch00", 0, "a.qptiff"), ("a#ch01", 1, "a.qptiff"),
                     ("b#ch00", 0, "b.qptiff"))
    assert checks.qptiff_channel_indices_are_contiguous(snap) == []


def test_a_record_missing_its_index_is_left_to_the_provenance_check():
    snap = _channels(("slide#ch00", None, QPTIFF))
    assert checks.qptiff_channel_indices_are_contiguous(snap) == []


# The declared set itself.
def test_every_declared_check_is_owner_scoped_and_described():
    for c in checks.pipeline_checks():
        assert c.name.startswith("pipeline."), c.name
        assert c.description, c.name
        assert c.severity in ("error", "warn"), c.name


def test_no_predicate_reports_anything_on_an_empty_instance():
    empty = _Snap()
    for c in checks.pipeline_checks():
        if c.needs_storage:
            continue
        assert list(c.test(empty)) == [], c.name


def test_the_data_tests_flow_names_this_repo_s_checks_explicitly():
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/qptiff_pipeline/flows/data_tests.py").read_text()
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "pipeline_checks" in names, "data_tests must name this repo's checks, not discover them"
    assert "framework_checks" in names, "data_tests must include the framework's invariants too"
    assert "all_checks" not in names, (
        "all_checks() discovers via entry points, which are absent in the SIF")


def test_a_check_over_a_non_roi_type_bulk_loads_it_first():
    import ast
    import pathlib

    # Vocab attribute names for non-ROI types, matched on the attribute, not the value.
    non_roi_attrs = {"QPTIFF_CHANNELS_TYPE"}
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/qptiff_pipeline/checks/pipeline.py").read_text()
    offenders = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        if attrs & non_roi_attrs and "load_values" not in attrs:
            offenders.append(fn.name)
    assert not offenders, f"iterate a non-ROI type without bulk-loading it: {offenders}"
