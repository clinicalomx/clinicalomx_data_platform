"""Tests for availability: the role states, the byte probe, degradation, and the report.

Nothing here connects: every Snapshot is pre-populated.
"""
from __future__ import annotations

import pytest

from clinicalomx import governance
from clinicalomx.extract import Element, Recipe
from clinicalomx.extract.availability import describe, summarise
from test_snapshot import _loaded, _Rec

SLIDE = _Rec("uS", "arutha", "WholeSlide")
TMA = _Rec("uT", "arutha:TMA0", "TMA")
CORES = [_Rec(f"uC{i}", f"arutha:TMA0:R0C{i}", "Core") for i in range(4)]

PLACEABLE = {"bbox_x1": 1.0, "bbox_y1": 1.0, "bbox_x2": 2.0, "bbox_y2": 2.0, "parquet_row": 0}

IMAGE = Element("image", "image", roles=("bgnormed", "source_image"),
                needs_features=tuple(PLACEABLE))
TABLE = Element("table", "table", roles=("cell_tables",), required=False)

CORE_RECIPE = Recipe(
    name="t.core", target="SpatialData", roi_types=("Core",), layout="single",
    elements=(IMAGE, TABLE), builder="m:b", placement="m:p")

MERGED = Recipe(
    name="t.merged", target="SpatialData", roi_types=("TMA",), layout="merged",
    child_types=("Core",),
    elements=(Element("image", "image", roles=("source_image",), level="children",
                      needs_features=tuple(PLACEABLE)),),
    builder="m:b", placement="m:p")

PER_CHILD = Recipe(
    name="t.per-child", target="SpatialData", roi_types=("TMA",), layout="per_child",
    child_types=("Core",), elements=(IMAGE, TABLE), builder="m:b", placement="m:p")


@pytest.fixture(autouse=True)
def _permissive_governance(monkeypatch):
    """Permit every folder, so keys do not all resolve `denied`."""
    monkeypatch.setattr(governance, "permits_read", lambda key: not key.startswith("secret/"))
    yield


def _snap(*, core_roles=None, tma_roles=(), values=None, declared=None, backrefs=None):
    """A Snapshot modelling a TMA with four cores. `core_roles` maps core index -> (role, key)."""
    vals = {"uT": {"parent_roi": "arutha"}}
    for c in CORES:
        vals[c.uid] = {"parent_roi": "arutha:TMA0", **PLACEABLE}
    vals.update(values or {})
    snap = _loaded({"WholeSlide": [SLIDE], "TMA": [TMA], "Core": CORES}, vals)

    triples = [(TMA, role, key) for role, key in tma_roles]
    for idx, entries in (core_roles or {}).items():
        triples += [(CORES[idx], role, key) for role, key in entries]
    snap._role_artifacts = triples
    snap._role_features = tuple(sorted(
        declared if declared is not None else {r for _x, r, _k in triples}))
    snap._backrefs = ({k: {"roi_uid": "x"} for _r, _f, k in triples}
                      if backrefs is None else backrefs)
    # Provenance backrefs stubbed to the same set, so the fixture stays offline.
    snap.artifact_rows = lambda keys, columns: {
        k: {"roi_uid": "x"} for k in keys if k in snap._backrefs}
    return snap


def _describe(snap, roi=TMA, recipes=(MERGED,), **kw):
    return describe(roi, snapshot=snap, recipes=list(recipes), now="2026-01-01T00:00:00Z", **kw)


def _cov(av, role):
    return next(c for c in av.child_coverage if c.role == role)


# The role states are distinct.
def test_a_role_no_installed_vocabulary_declares_is_undeclared_not_absent():
    av = _describe(_snap(declared=set()), recipes=(PER_CHILD,))
    assert _cov(av, "cell_tables").state == "undeclared"


def test_foundation_provenance_is_always_declared():
    av = _describe(_snap(declared=set()), recipes=(PER_CHILD,))
    assert _cov(av, "source_image").state != "undeclared"


def test_a_declared_role_with_no_artifact_is_unlinked():
    av = _describe(_snap(declared={"source_image"}))
    assert _cov(av, "source_image").state == "unlinked"


def test_a_value_with_no_registered_artifact_is_dangling_not_unlinked():
    snap = _snap(core_roles={0: [("source_image", "img/a.tif")]}, backrefs={})
    assert _cov(_describe(snap), "source_image").dangling == 1


def test_governance_denial_is_reported_as_denied_not_missing():
    snap = _snap(core_roles={0: [("source_image", "secret/a.tif")],
                             1: [("source_image", "img/b.tif")]})
    cov = _cov(_describe(snap), "source_image")
    assert cov.denied == 1 and cov.missing == 0 and cov.present == 1


def test_present_wins_when_one_of_several_artifacts_is_denied():
    snap = _snap(core_roles={0: [("source_image", "secret/a.tif"),
                                 ("source_image", "img/a.tif")]})
    assert _cov(_describe(snap), "source_image").present == 1


# Bytes were not probed, so say so.
def test_missing_is_never_claimed_when_bytes_were_not_probed():
    av = _describe(_snap(core_roles={0: [("source_image", "img/a.tif")]}))
    assert av.bytes_checked is False
    assert "MISSING" in summarise(av)
    assert _cov(av, "source_image").missing == 0
    line = next(ln for ln in summarise(av).splitlines() if ln.strip().startswith("source_image"))
    assert "-" in line, "the MISSING column must print '-' when nothing was probed"


def test_a_catalogue_only_read_never_reaches_storage():
    class _Unreachable:
        def artifacts(self, keys):
            raise OSError("this host cannot reach the artifact storage")

    snap = _snap(core_roles={0: [("source_image", "img/a.tif")],
                             1: [("source_image", "img/b.tif")]})
    av = _describe(snap, source=_Unreachable())
    cov = _cov(av, "source_image")
    assert av.bytes_checked is False
    assert cov.present == 2 and cov.missing == 0
    assert "missing" not in cov.examples
    assert av.recipes[0].verdict != "blocked"


def test_the_unverified_caveat_is_present_by_default():
    av = _describe(_snap(core_roles={0: [("source_image", "img/a.tif")]}))
    assert any("bytes were NOT verified" in c for c in av.caveats())


def test_the_undeclared_caveat_names_the_roles_and_refuses_to_over_claim():
    av = _describe(_snap(declared=set()), recipes=(PER_CHILD,))
    caveat = next(c for c in av.caveats() if "undeclared" in c)
    assert "cell_tables" in caveat
    assert "not proof nothing produces them" in caveat


def test_graph_warnings_reach_the_caveats():
    snap = _snap(values={"uC0": {"parent_roi": "arutha:TMA9", **PLACEABLE}})
    av = _describe(snap)
    assert any("TMA9" in c for c in av.caveats())


# Degradation versus blocking.
def test_a_partially_covered_required_element_degrades_rather_than_blocks():
    snap = _snap(core_roles={0: [("source_image", "img/a.tif")],
                             1: [("source_image", "img/b.tif")]})
    r = _describe(snap).recipes[0]
    assert r.verdict == "degraded" and r.n_units == 1


def test_a_required_element_nothing_supplies_blocks():
    r = _describe(_snap(declared={"source_image"})).recipes[0]
    assert r.verdict == "blocked" and r.n_units == 0


def test_an_absent_optional_element_degrades_but_still_builds():
    snap = _snap(core_roles={i: [("source_image", f"img/{i}.tif")] for i in range(4)},
                 declared={"source_image", "cell_tables"})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict == "degraded" and r.n_units == 1
    assert [e.element for e in r.degraded] == ["table"]


def test_everything_present_is_ready():
    snap = _snap(core_roles={i: [("source_image", f"img/{i}.tif"),
                                 ("cell_tables", f"tab/{i}.h5ad")] for i in range(4)})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict == "ready" and not r.blocking and not r.degraded


def test_one_unplaceable_core_degrades_rather_than_blocking_the_rest():
    snap = _snap(
        core_roles={i: [("source_image", f"img/{i}.tif")] for i in range(4)},
        values={"uC3": {"parent_roi": "arutha:TMA0"}})   # no bbox: present but unplaceable
    r = _describe(snap).recipes[0]
    assert r.verdict == "degraded"
    assert r.elements[0].unplaceable == ("arutha:TMA0:R0C3",)
    assert r.elements[0].coverage.unplaceable == 1
    assert r.elements[0].coverage.present == 3, "the unplaceable core is not counted as usable"


def test_placeability_is_an_element_property_not_a_role_one():
    snap = _snap(
        core_roles={i: [("source_image", f"img/{i}.tif")] for i in range(4)},
        values={"uC3": {"parent_roi": "arutha:TMA0"}})
    assert _cov(_describe(snap), "source_image").present == 4
    assert _describe(snap).recipes[0].elements[0].coverage.present == 3


# A declared needs_features that is never written.
def test_an_artifact_that_cannot_be_placed_does_not_report_as_absent():
    from clinicalomx.extract.availability import _WHY

    snap = _realistic({0: [("source_image", "img/slide.qptiff")]},
                      values={"uC0": {"parent_roi": "arutha:TMA0"}})
    assert snap.artifact_keys(CORES[0], "source_image"), "the artifact IS there"

    element = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0].elements[0]
    assert element.state == "unplaceable"
    assert "artifact IS present" in _WHY[element.state]
    assert element.state != "unlinked", "saying 'no artifact' about a present artifact is the bug"


def test_a_few_unplaceable_children_do_not_make_the_element_unplaceable():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)},
                      values={"uC3": {"parent_roi": "arutha:TMA0"}})
    element = _describe(snap, recipes=(PER_CHILD,)).recipes[0].elements[0]
    assert element.state == "present"
    assert element.coverage.unplaceable == 1


def test_the_corpus_view_keeps_the_reason_rather_than_collapsing_to_blocked():
    from clinicalomx.extract.corpus import survey

    snap = _realistic(
        {i: [("source_image", "img/slide.qptiff")] for i in range(4)},
        values={f"uC{i}": {"parent_roi": "arutha:TMA0"} for i in range(4)})
    reach = {r.recipe: r for r in survey(snapshot=snap, roi_types=["Core"],
                                         recipes=[CORE_RECIPE], now="2026-01-01T00:00:00Z").recipes}
    states = reach["t.core"].element_states["image"]
    assert states.get("unplaceable") == 4, "every core unplaceable, and the survey says so"
    assert reach["t.core"].declaration_suspects(min_n=4) == ("image",)


def test_a_data_gap_is_not_reported_as_a_declaration_fault():
    from clinicalomx.extract.corpus import survey

    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)})
    reach = {r.recipe: r for r in survey(snapshot=snap, roi_types=["Core"],
                                         recipes=[CORE_RECIPE], now="2026-01-01T00:00:00Z").recipes}
    assert "table" not in reach["t.core"].declaration_suspects(min_n=4)


def test_an_unplaceable_sole_target_does_block():
    snap = _snap(core_roles={0: [("source_image", "img/0.tif")]},
                 values={"uC0": {"parent_roi": "arutha:TMA0"}})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict == "blocked"


# The layout axis.
def test_per_child_resolves_self_against_each_child_not_the_parent():
    snap = _snap(core_roles={i: [("source_image", f"img/{i}.tif")] for i in range(4)})
    r = _describe(snap, recipes=(PER_CHILD,)).recipes[0]
    assert r.verdict != "blocked"
    assert r.elements[0].chosen_role == "source_image"


def test_merged_builds_one_unit_and_per_child_builds_one_per_core():
    snap = _snap(core_roles={i: [("source_image", f"img/{i}.tif")] for i in range(4)})
    assert _describe(snap, recipes=(MERGED,)).recipes[0].n_units == 1
    assert _describe(snap, recipes=(PER_CHILD,)).recipes[0].n_units == 4


def test_per_child_counts_only_the_children_it_could_actually_build():
    snap = _snap(core_roles={0: [("source_image", "img/0.tif")],
                             1: [("source_image", "img/1.tif")]})
    assert _describe(snap, recipes=(PER_CHILD,)).recipes[0].n_units == 2


def test_a_merged_recipe_says_where_its_output_will_be_anchored():
    notes = " ".join(_describe(_snap(), recipes=(MERGED,)).recipes[0].notes)
    assert "anchors to this TMA" in notes and "walking up to its parent" in notes


# The degradation ladder, with `source_image` as provenance rather than a role.
def _realistic(core_roles, *, values=None):
    """A Snapshot whose `role_features` excludes the foundation's provenance features, as live."""
    snap = _snap(core_roles=core_roles, values=values, declared={"bgnormed"})
    return snap


def test_a_core_with_no_bgnormed_falls_back_to_the_raw_image():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict != "blocked", "a core with only a raw image must still be buildable"
    assert r.elements[0].chosen_role == "source_image"


def test_the_raw_image_fallback_works_for_a_whole_tma():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)})
    r = _describe(snap, recipes=(MERGED,)).recipes[0]
    assert r.verdict != "blocked" and r.n_units == 1


def test_a_core_with_bgnormed_still_prefers_it_over_the_raw_image():
    snap = _realistic({0: [("bgnormed", "n/0.zarr"), ("source_image", "img/slide.qptiff")]})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.elements[0].chosen_role == "bgnormed"


def test_a_mixed_tma_reports_each_core_by_the_role_that_would_actually_be_used():
    snap = _realistic({
        0: [("bgnormed", "n/0.zarr")], 1: [("bgnormed", "n/1.zarr")],
        2: [("source_image", "img/slide.qptiff")], 3: [("source_image", "img/slide.qptiff")]})
    r = _describe(snap, recipes=(PER_CHILD,)).recipes[0]
    assert r.n_units == 4, "every core is buildable; two just use the raw image"
    assert r.elements[0].roles_used == {"bgnormed": 2, "source_image": 2}


def test_a_mixed_element_does_not_claim_every_target_uses_the_better_role():
    snap = _realistic({
        0: [("bgnormed", "n/0.zarr")],
        **{i: [("source_image", "img/slide.qptiff")] for i in range(1, 4)}})
    av = _describe(snap, recipes=(PER_CHILD,))
    assert av.recipes[0].elements[0].roles_used == {"bgnormed": 1, "source_image": 3}
    assert "bgnormed x1, source_image x3" in summarise(av)


def test_each_core_is_recorded_with_the_role_that_will_actually_fill_it():
    snap = _realistic({
        0: [("bgnormed", "n/0.zarr")],
        **{i: [("source_image", "img/slide.qptiff")] for i in range(1, 4)}})
    image = _describe(snap, recipes=(PER_CHILD,)).recipes[0].elements[0]
    assert image.role_for("arutha:TMA0:R0C0") == "bgnormed"
    assert image.role_for("arutha:TMA0:R0C2") == "source_image"


#: A merged recipe whose image element offers the real degradation ladder.
MERGED_LADDER = Recipe(
    name="t.merged.ladder", target="SpatialData", roi_types=("TMA",), layout="merged",
    child_types=("Core",),
    elements=(Element("image", "image", roles=("bgnormed", "source_image"), level="children",
                      needs_features=tuple(PLACEABLE)),),
    builder="m:b", placement="m:p")


def test_a_merged_object_records_a_role_per_child_too():
    snap = _realistic({
        0: [("bgnormed", "n/0.zarr")],
        **{i: [("source_image", "img/slide.qptiff")] for i in range(1, 4)}})
    image = _describe(snap, recipes=(MERGED_LADDER,)).recipes[0].elements[0]
    assert image.per_target_role["arutha:TMA0:R0C0"] == "bgnormed"
    assert image.per_target_role["arutha:TMA0:R0C3"] == "source_image"
    assert image.roles_used == {"bgnormed": 1, "source_image": 3}


# requires_any.
SERIES = Recipe(
    name="t.series", target="OME-TIFF", roi_types=("Core",), layout="single",
    elements=(
        Element("raw", "image", roles=("source_image",), required=False,
                needs_features=tuple(PLACEABLE)),
        Element("bgnorm", "image", roles=("bgnormed",), variable="adjusted_image",
                required=False, needs_features=tuple(PLACEABLE)),
        Element("bgnormQ", "image", roles=("bgnormed",), variable="adjusted_image_post_hoc",
                required=False, needs_features=tuple(PLACEABLE)),
    ),
    requires_any=("raw", "bgnorm", "bgnormQ"),
    builder="m:b", placement="m:p")


def test_all_three_series_resolve_when_the_core_has_been_normalised():
    snap = _realistic({0: [("bgnormed", "n/0.zarr"), ("source_image", "img/s.qptiff")]})
    r = _describe(snap, roi=CORES[0], recipes=(SERIES,)).recipes[0]
    assert r.verdict == "ready"
    assert [e.element for e in r.elements if e.satisfied] == ["raw", "bgnorm", "bgnormQ"]


def test_only_raw_resolves_when_the_core_has_not_been_normalised():
    snap = _realistic({0: [("source_image", "img/s.qptiff")]})
    r = _describe(snap, roi=CORES[0], recipes=(SERIES,)).recipes[0]
    assert r.verdict != "blocked"
    assert [e.element for e in r.elements if e.satisfied] == ["raw"]


def test_none_of_the_group_present_blocks_rather_than_writing_an_empty_file():
    r = _describe(_realistic({}), roi=CORES[0], recipes=(SERIES,)).recipes[0]
    assert r.verdict == "blocked"
    assert {e.element for e in r.blocking} == {"raw", "bgnorm", "bgnormQ"}


def test_the_two_bgnorm_series_come_from_one_artifact_by_variable():
    snap = _realistic({0: [("bgnormed", "n/0.zarr")]})
    r = _describe(snap, roi=CORES[0], recipes=(SERIES,)).recipes[0]
    both = {e.element: e for e in r.elements if e.satisfied}
    assert both["bgnorm"].chosen_role == both["bgnormQ"].chosen_role == "bgnormed"


def test_chosen_role_follows_the_recipe_preference_not_the_first_target():
    snap = _realistic({
        0: [("source_image", "img/slide.qptiff")], 1: [("bgnormed", "n/1.zarr")]})
    assert _describe(snap, recipes=(PER_CHILD,)).recipes[0].elements[0].chosen_role == "bgnormed"


def test_one_raw_image_shared_by_every_core_is_not_a_fault():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)})
    cov = _cov(_describe(snap), "source_image")
    assert cov.present == 4 and cov.state == "present"


def test_a_core_with_neither_still_blocks():
    snap = _realistic({})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict == "blocked"


def test_degradation_prefers_the_better_role_when_both_are_present():
    snap = _snap(core_roles={0: [("bgnormed", "img/n.zarr"), ("source_image", "img/r.tif")]},
                 declared={"bgnormed", "source_image"})
    r = _describe(snap, roi=CORES[0], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.elements[0].chosen_role == "bgnormed"


# Report shape.
def test_the_report_is_json_serialisable():
    import json

    av = _describe(_snap(core_roles={0: [("source_image", "img/a.tif")]}))
    json.dumps(av.to_dict())


def test_the_report_names_the_instances_not_just_a_count():
    snap = _snap(core_roles={0: [("source_image", "img/a.tif")]})
    assert "arutha:TMA0:R0C1" in _cov(_describe(snap), "source_image").examples["unlinked"]


def test_the_report_states_which_vocabularies_were_visible():
    av = _describe(_snap())
    assert av.installed_consumers
    assert "vocabularies visible" in summarise(av)


def test_the_subtree_is_counted_by_type():
    assert _describe(_snap()).subtree == {"TMA": 1, "Core": 4}


def test_describe_issues_no_write():
    _describe(_snap(core_roles={0: [("source_image", "img/a.tif")]}))


# `raw` is always reachable: one qptiff is scanned per slide.
def test_raw_is_available_for_a_core_carrying_no_artifact_of_its_own():
    from clinicalomx.extract.app import demo
    from clinicalomx.extract.availability import describe

    demo.install_governance()
    snap = demo.build_snapshot()
    core = snap.record_by_name("Core", f"{demo.TMA_NAME}:R0C{demo.EMPTY_CORE}")
    assert not snap.roles_of(core), "the fixture core is no longer artifact-free"

    av = describe(core, snapshot=snap, now="2026-01-01T00:00:00Z")
    raw = next(e for r in av.recipes if r.recipe == "ometiff.core"
               for e in r.elements if e.element == "raw")
    assert raw.satisfied
    assert raw.chosen_role == "source_image"


def test_the_resolver_and_the_builder_inherit_the_same_features():
    import inspect

    from clinicalomx.extract import availability
    from clinicalomx.extract.builders import ometiff

    for module in (availability, ometiff):
        assert "INHERITED_FEATURES" in inspect.getsource(module), module.__name__


def test_inheritance_walks_the_whole_spine_not_one_level():
    from clinicalomx.extract.app import demo

    demo.install_governance()
    snap = demo.build_snapshot()
    core = snap.record_by_name("Core", f"{demo.TMA_NAME}:R0C{demo.EMPTY_CORE}")
    tma = snap.parent(core)

    assert not snap.artifact_keys(tma, "source_image"), "the TMA would satisfy a one-level hop"
    assert snap.artifact_keys(core, "source_image", inherit=True), "the slide was not reached"


def test_only_source_image_is_inherited():
    from clinicalomx.snapshot import INHERITED_FEATURES

    assert INHERITED_FEATURES == ("source_image",)


def test_inheritance_is_off_by_default():
    from clinicalomx.extract.app import demo

    demo.install_governance()
    snap = demo.build_snapshot()
    core = snap.record_by_name("Core", f"{demo.TMA_NAME}:R0C{demo.EMPTY_CORE}")
    assert snap.artifact_keys(core, "source_image") == ()


# An inferred grid cell is not a core, so it is not a gap.
#: `uC3` is an inferred grid cell: no detector found tissue there.
INFERRED = {"uC3": {"parent_roi": "arutha:TMA0", **PLACEABLE, "is_detected": False}}

BGNORM_ONLY = Element("image", "image", roles=("bgnormed",), needs_features=tuple(PLACEABLE))
BGNORM_RECIPE = Recipe(
    name="t.bgnorm", target="SpatialData", roi_types=("TMA",), layout="per_child",
    child_types=("Core",), elements=(BGNORM_ONLY,), builder="m:b", placement="m:p")


def test_an_inferred_grid_cell_is_not_reported_as_work_outstanding():
    snap = _snap(core_roles={i: [("bgnormed", f"bg/{i}.zarr")] for i in range(3)},
                 values=INFERRED, declared={"bgnormed"})
    cov = _describe(snap, recipes=(BGNORM_RECIPE,)).recipes[0].elements[0].coverage
    assert cov.fraction == "3/3"
    assert cov.unlinked == 0, "the inferred cell was counted as a missing artifact"


def test_a_detected_core_with_no_artifact_still_reads_as_work_outstanding():
    snap = _snap(core_roles={i: [("bgnormed", f"bg/{i}.zarr")] for i in range(3)},
                 declared={"bgnormed"})
    cov = _describe(snap, recipes=(BGNORM_RECIPE,)).recipes[0].elements[0].coverage
    assert cov.fraction == "3/4"
    assert cov.unlinked == 1
    assert cov.examples["unlinked"] == ("arutha:TMA0:R0C3",)


def test_the_exclusion_is_reported_rather_than_a_quietly_smaller_denominator():
    snap = _snap(core_roles={i: [("bgnormed", f"bg/{i}.zarr")] for i in range(3)},
                 values=INFERRED, declared={"bgnormed"})
    av = _describe(snap, recipes=(BGNORM_RECIPE,))
    assert av.subtree["Core"] == 4
    assert av.inferred_cells == ("arutha:TMA0:R0C3",)
    assert any("INFERRED grid cells" in c for c in av.caveats())
    assert "inferred grid cell(s) excluded" in summarise(av)


def test_availability_and_the_builders_walk_the_same_ROIs():
    snap = _snap(core_roles={i: [("bgnormed", f"bg/{i}.zarr")] for i in range(3)},
                 values=INFERRED, declared={"bgnormed"})
    walked = BGNORM_RECIPE.targets_under(TMA, snap)
    cov = _describe(snap, recipes=(BGNORM_RECIPE,)).recipes[0].elements[0].coverage
    assert cov.total == len(walked) == 3
    assert "arutha:TMA0:R0C3" not in {str(c.name) for c in walked}


def test_the_raw_fallback_does_not_smuggle_an_inferred_cell_back_in():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)},
                      values=INFERRED)
    element = _describe(snap, recipes=(PER_CHILD,)).recipes[0].elements[0]
    assert element.coverage.fraction == "3/3"
    assert element.coverage.unplaceable == 0
    assert element.unplaceable == ()


def test_a_plan_over_an_inferred_cell_is_blocked_and_says_why():
    snap = _realistic({i: [("source_image", "img/slide.qptiff")] for i in range(4)},
                      values=INFERRED)
    r = _describe(snap, roi=CORES[3], recipes=(CORE_RECIPE,)).recipes[0]
    assert r.verdict == "blocked" and r.n_units == 0
    assert any("it is not a core" in n for n in r.notes)


def test_the_corpus_view_excludes_inferred_cells_from_its_coverage_too():
    from clinicalomx.extract.corpus import survey

    snap = _snap(core_roles={i: [("bgnormed", f"bg/{i}.zarr")] for i in range(3)},
                 values=INFERRED, declared={"bgnormed"})
    sv = survey(snapshot=snap, roi_types=["Core"], recipes=[CORE_RECIPE],
                now="2026-01-01T00:00:00Z")
    assert next(c for c in sv.coverage if c.role == "bgnormed").fraction == "3/3"
    assert sv.population["Core"] == 4, "the instance does hold four Core records"
    assert sv.inferred_cells == 1
    assert any("INFERRED grid cells" in c for c in sv.caveats())


# Elements, not roles: what the catalogue can address.
#: Two elements over one store plus a third over another, read from every child.
TMA_SERIES = Recipe(
    name="t.series", target="OME-TIFF", roi_types=("TMA",), layout="per_child",
    child_types=("Core",),
    elements=(
        Element("raw", "image", roles=("source_image",), required=False,
                needs_features=tuple(PLACEABLE)),
        Element("bgnorm", "image", roles=("bgnormed",), variable="adjusted_image",
                required=False, needs_features=tuple(PLACEABLE)),
        Element("bgnormQ", "image", roles=("bgnormed",), variable="adjusted_image_post_hoc",
                required=False, needs_features=tuple(PLACEABLE)),
    ),
    requires_any=("raw", "bgnorm", "bgnormQ"),
    builder="m:b", placement="m:p")

#: The same three elements, declared by a second recipe.
TMA_SERIES_MERGED = Recipe(
    name="t.series.merged", target="OME-TIFF", roi_types=("TMA",), layout="merged",
    child_types=("Core",), aggregates=True,
    elements=tuple(
        Element(e.name, e.kind, roles=e.roles, variable=e.variable, required=False,
                level="children", needs_features=e.needs_features)
        for e in TMA_SERIES.elements),
    requires_any=("raw", "bgnorm", "bgnormQ"),
    builder="m:b", placement="m:p")


def _series_snap():
    """Two cores normalised, two not. Every core shares one qptiff."""
    roles = {i: [("source_image", "img/slide.qptiff")] for i in range(4)}
    for i in (0, 1):
        roles[i] = [("bgnormed", f"n/{i}.zarr"), *roles[i]]
    return _snap(core_roles=roles, declared={"bgnormed", "source_image"})


def _element(av, name):
    return next(e for e in av.child_elements if e.element == name)


def test_two_elements_over_one_store_are_two_entries_and_one_store():
    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    assert {e.element for e in av.child_elements} == {"raw", "bgnorm", "bgnormQ"}
    assert _element(av, "bgnorm").variable == "adjusted_image"
    assert _element(av, "bgnormQ").variable == "adjusted_image_post_hoc"
    assert _element(av, "bgnorm").keys == _element(av, "bgnormQ").keys


def test_a_shared_store_is_counted_once_however_many_elements_address_it():
    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    over_elements = sum(len(e.keys) for e in av.child_elements if e.chosen_role == "bgnormed")
    assert over_elements == 4, "two elements x two normalised cores"
    bgnormed = {k for k in av.child_stores if k.startswith("n/")}
    assert len(bgnormed) == 2, "and the same bytes, counted once"


def test_the_shared_store_relationship_is_reported_rather_than_left_to_be_inferred():
    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    assert av.shares_store(_element(av, "bgnorm")) == ("bgnormQ",)
    assert av.shares_store(_element(av, "bgnormQ")) == ("bgnorm",)
    assert av.shares_store(_element(av, "raw")) == ()


def test_an_element_several_recipes_declare_is_one_entry_not_one_per_recipe():
    av = _describe(_series_snap(), recipes=(TMA_SERIES, TMA_SERIES_MERGED))
    assert [e.element for e in av.child_elements].count("bgnorm") == 1


def test_every_element_shares_one_denominator_with_the_role_table():
    av = _describe(_series_snap(), recipes=(TMA_SERIES, TMA_SERIES_MERGED))
    total = av.child_coverage[0].total
    assert {e.coverage.total for e in av.child_elements} == {total}


def test_the_element_view_does_not_move_a_role_coverage_number():
    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    assert (_cov(av, "bgnormed").present, _cov(av, "bgnormed").total) == (2, 4)
    assert _cov(av, "source_image").present == 4


def test_an_element_read_from_the_anchor_is_not_a_child_element():
    grid = Recipe(
        name="t.grid", target="SpatialData", roi_types=("TMA",), layout="merged",
        child_types=("Core",),
        elements=(Element("cores", "shapes", roles=("cores_accepted",)),
                  Element("raw", "image", roles=("source_image",), level="children",
                          required=False, needs_features=tuple(PLACEABLE))),
        builder="m:b", placement="m:p")
    av = _describe(_series_snap(), recipes=(grid,))
    assert [e.element for e in av.child_elements] == ["raw"]


def test_a_role_nothing_addresses_still_reaches_the_role_table():
    snap = _snap(core_roles={0: [("roi_thumbnail", "review/0.png")]},
                 declared={"roi_thumbnail", "source_image"})
    av = _describe(snap, recipes=(TMA_SERIES,))
    assert "roi_thumbnail" not in {e.chosen_role for e in av.child_elements}
    assert _cov(av, "roi_thumbnail").present == 1


def test_the_element_view_reaches_the_json():
    import json

    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    blob = json.loads(json.dumps(av.to_dict()))
    assert {e["element"] for e in blob["child_elements"]} == {"raw", "bgnorm", "bgnormQ"}
    assert sorted(blob["child_stores"]["n/0.zarr"]) == ["bgnorm", "bgnormQ"]


def test_the_report_prints_elements_with_their_variable_and_what_they_share():
    text = summarise(_describe(_series_snap(), recipes=(TMA_SERIES,)))
    assert "addressable on 4 child ROI(s)" in text
    assert "adjusted_image_post_hoc" in text
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("bgnorm "))
    assert "bgnormQ" in line, "the shared store is named on the row that shares it"


# What a request could name.
def _named(pool):
    """`{(store, variable): entry}`."""
    from clinicalomx.extract.availability import _store_of

    return {(_store_of(e), e.variable): e for e in pool}


def test_each_array_of_a_shared_store_is_separately_addressable():
    from clinicalomx.extract.availability import addressable

    pool = _named(addressable(_describe(_series_snap(), recipes=(TMA_SERIES,))))
    assert ("bgnormed", "adjusted_image") in pool
    assert ("bgnormed", "adjusted_image_post_hoc") in pool
    assert pool[("bgnormed", "adjusted_image")].kind == "image"


def test_a_role_nothing_addresses_is_offered_whole_with_its_coverage():
    from clinicalomx.extract.availability import addressable

    snap = _snap(core_roles={0: [("roi_thumbnail", "review/0.png")]},
                 declared={"roi_thumbnail", "source_image"})
    pool = _named(addressable(_describe(snap, recipes=(TMA_SERIES,))))
    assert pool[("roi_thumbnail", None)].coverage.fraction == "1/4"


def test_a_store_an_element_addresses_is_not_also_offered_whole():
    from clinicalomx.extract.availability import addressable

    pool = _named(addressable(_describe(_series_snap(), recipes=(TMA_SERIES,))))
    assert ("bgnormed", None) not in pool


def test_the_caller_says_how_its_recipe_reaches_a_child():
    from clinicalomx.extract.availability import addressable

    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    assert {e.level for e in addressable(av, child_level="children")} == {"children"}
    assert not [e for e in addressable(av, child_level=None) if e.chosen_role == "bgnormed"]


def test_the_anchors_own_artifacts_are_addressable_where_the_units_are_not_its_children():
    from clinicalomx.extract.availability import addressable

    snap = _snap(core_roles={}, tma_roles=[("cores_accepted", "det/grid.parquet")],
                 declared={"cores_accepted", "source_image", "bgnormed"})
    av = _describe(snap, recipes=(TMA_SERIES,))
    assert ("cores_accepted", None) in _named(addressable(av, child_level="children"))
    assert ("cores_accepted", None) not in _named(addressable(av, child_level="self"))


# Looking inside a store.
class _FakeSource:
    """A Source that reports a path for every key it is asked about."""

    def __init__(self, uris):
        self.uris, self.asked = uris, []

    def artifacts(self, ids):
        from clinicalomx.extract.source import Artifact

        self.asked += list(ids)
        return [Artifact(artifact_id=k, uri=self.uris[k], stored=True)
                for k in ids if k in self.uris]


def test_one_store_is_opened_per_artifact_not_one_per_roi():
    from clinicalomx.extract.availability import inspect_stores

    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    src = _FakeSource({"n/0.zarr": "/tmp/n0.zarr", "img/slide.qptiff": "/tmp/s.qptiff"})
    inspect_stores(av, source=src)
    # Two roles, one key each.
    assert sorted(src.asked) == ["img/slide.qptiff", "n/0.zarr"]


def test_a_forbidden_store_is_reported_denied_rather_than_opened(monkeypatch):
    from clinicalomx.extract.availability import inspect_stores

    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    monkeypatch.setattr(governance, "permits_read", lambda _key: False)
    src = _FakeSource({"n/0.zarr": "/tmp/n0.zarr", "img/slide.qptiff": "/tmp/s.qptiff"})
    contents = inspect_stores(av, source=src)["bgnormed"]
    assert not contents.ok and "governance" in contents.error
    assert src.asked == [], "the path was never even resolved"


def test_a_store_with_no_registered_row_is_reported_rather_than_opened():
    from clinicalomx.extract.availability import inspect_stores

    av = _describe(_series_snap(), recipes=(TMA_SERIES,))
    contents = inspect_stores(av, source=_FakeSource({}))["bgnormed"]
    assert not contents.ok
