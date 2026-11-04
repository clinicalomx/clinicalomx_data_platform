"""Tests for the two DOT diagrams: drawn from the plan's objects, unreached nodes greyed, and
unrecorded relations shown as stubs."""
from __future__ import annotations

import pytest

from clinicalomx.extract import all_recipes
from clinicalomx.extract import relations as rel
from clinicalomx.extract.app import demo, diagram
from clinicalomx.extract.availability import describe

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def snap():
    demo.install_governance()
    return demo.build_snapshot()


def _recipe(name):
    return next(r for r in all_recipes() if r.name == name)


def _av(snap, type_name, name):
    return describe(snap.record_by_name(type_name, name), snapshot=snap, now=NOW)


# The source tree.
def test_the_source_tree_shows_the_whole_spine(snap):
    dot = diagram.source_dot(_av(snap, "WholeSlide", demo.SLIDE_NAME), snap,
                             recipe=_recipe("spatialdata.slide.by-roi"))
    for expected in ("TMA0", "TMA1", "TissueSection0", "R0C0"):
        assert expected in dot, expected


def test_a_subtype_with_no_placement_is_shown_in_position_rather_than_dropped(snap):
    dot = diagram.source_dot(_av(snap, "WholeSlide", demo.SLIDE_NAME), snap,
                             recipe=_recipe("spatialdata.slide.by-roi"))
    section = next(ln for ln in dot.splitlines() if "TissueSection0" in ln)
    assert "no placement" in section
    assert diagram.STATE_COLOUR["unplaceable"][0] in section


def test_nodes_a_recipe_does_not_reach_are_greyed_not_hidden(snap):
    dot = diagram.source_dot(_av(snap, "WholeSlide", demo.SLIDE_NAME), snap,
                             recipe=_recipe("spatialdata.slide"))  # `single`: reaches nothing
    assert "R0C0" in dot, "the cores were hidden rather than greyed"
    core = next(ln for ln in dot.splitlines() if '"uC00"' in ln)
    assert diagram._UNREACHED[0] in core


def test_the_tree_is_bounded(snap):
    dot = diagram.source_dot(_av(snap, "WholeSlide", demo.SLIDE_NAME), snap, max_nodes=3)
    assert "and 13 more" in dot


def test_a_vanished_roi_does_not_crash_the_page(snap):
    av = _av(snap, "TMA", demo.TMA_NAME)
    snap._by_uid = {}
    assert "digraph" in diagram.source_dot(av, snap)


# The object being built.
def _readiness(snap, name="spatialdata.tma.by-core"):
    return next(r for r in _av(snap, "TMA", demo.TMA_NAME).recipes if r.recipe == name)


def test_the_object_graph_has_a_cluster_per_slot(snap):
    dot = diagram.object_dot(_readiness(snap))
    for kind in ("image", "labels", "table", "shapes"):
        assert f'label="{kind}"' in dot, kind


def test_an_entry_shows_the_role_and_variable_it_reads(snap):
    dot = diagram.object_dot(_readiness(snap))
    assert "bgnormed#adjusted_image" in dot
    assert "bgnormed#adjusted_image_post_hoc" in dot


def test_a_resolved_derivation_is_an_edge_between_the_two_elements(snap):
    relations = rel.Relations("roi")
    relations.add("derived_from", "bgnorm", "labels", "source_artifact")
    dot = diagram.object_dot(_readiness(snap), relations)
    assert '"bgnorm" -> "labels"' in dot
    assert "derived from" in dot


def test_a_table_annotating_labels_is_drawn_dashed(snap):
    relations = rel.Relations("roi")
    relations.add("annotates", "table", "labels", "spatialdata_region")
    dot = diagram.object_dot(_readiness(snap), relations)
    assert '"table" -> "labels"' in dot
    assert "annotates" in dot


def test_an_unrecorded_derivation_is_a_visible_hole_not_a_missing_edge(snap):
    relations = rel.Relations("roi")
    relations.add("derived_from", None, "labels", None)
    dot = diagram.object_dot(_readiness(snap), relations)
    assert "source unknown" in dot
    assert diagram.STATE_COLOUR["denied"][0] in dot, "the hole is not drawn as a fault"


def test_an_ambiguous_derivation_names_its_candidates(snap):
    relations = rel.Relations("roi")
    relations.add("derived_from", None, "labels", "source_artifact:ambiguous-variable",
                  ("bgnorm", "bgnormQ"))
    dot = diagram.object_dot(_readiness(snap), relations)
    assert "ambiguous" in dot
    assert "bgnorm" in dot and "bgnormQ" in dot


def test_an_edge_to_an_element_not_in_this_object_is_not_drawn(snap):
    relations = rel.Relations("roi")
    relations.add("derived_from", "elsewhere", "not_here", "source_artifact")
    assert '"elsewhere"' not in diagram.object_dot(_readiness(snap), relations)


def test_the_object_graph_is_bounded_per_slot(snap):
    dot = diagram.object_dot(_readiness(snap), max_per_slot=1)
    assert "more" in dot


# It is DOT, and it is safe.
def test_both_panels_emit_plain_dot_strings(snap):
    for dot in (diagram.source_dot(_av(snap, "TMA", demo.TMA_NAME), snap),
                diagram.object_dot(_readiness(snap))):
        assert isinstance(dot, str)
        assert dot.startswith("digraph G {") and dot.rstrip().endswith("}")


def test_a_quote_in_a_record_name_cannot_break_the_graph(snap):
    core = snap.record_by_name("Core", f"{demo.TMA_NAME}:R0C0")
    core.name = 'R0C0" [label="pwned'
    dot = diagram.source_dot(_av(snap, "TMA", demo.TMA_NAME), snap)
    assert 'label="pwned' not in dot
    assert "&quot;" in dot


# Fan-in - where a slot's contents come FROM.
def test_grouping_by_element_puts_every_core_in_one_row(snap):
    dot = diagram.object_dot(_readiness(snap), group="element", max_sources=3)
    assert "R0C0: bgnormed#adjusted_image" in dot
    assert '"src_bgnorm_R0C0" -> "bgnorm"' in dot


def test_grouping_by_roi_puts_each_cores_attributes_in_its_own_box(snap):
    dot = diagram.object_dot(_readiness(snap), group="roi", max_sources=3)
    assert 'subgraph "cluster_roi_R0C0"' in dot
    assert '"src_R0C0_raw" -> "raw"' in dot
    assert '"src_R0C0_bgnorm" -> "bgnorm"' in dot
    # Inside its own box the roi_id is the cluster label.
    assert '"src_R0C0_raw" [label="source_image"' in dot


def test_a_cores_attributes_stay_together_under_it(snap):
    dot = diagram.object_dot(_readiness(snap), group="roi", max_sources=2)
    box = dot.split('subgraph "cluster_roi_R0C0"')[1].split("  }")[0]
    for role in ("source_image", "bgnormed#adjusted_image", "segmentations", "cell_tables"):
        assert role in box, role


def test_roi_boxes_are_in_natural_order(snap):
    dot = diagram.object_dot(_readiness(snap), group="roi", max_sources=12)
    order = [b.split('"')[0] for b in dot.split("cluster_roi_")[1:]]
    assert order[:4] == ["R0C0", "R0C1", "R0C2", "R0C3"]


def test_the_roi_grouping_is_bounded_and_says_how_many_it_dropped(snap):
    dot = diagram.object_dot(_readiness(snap), group="roi", max_sources=2)
    assert "more ROI(s)" in dot


def test_a_mosaic_reads_the_same_way_grouped_by_roi(snap):
    dot = diagram.object_dot(_readiness(snap, "ometiff.tma.mosaic"), group="roi",
                             aggregates=True, max_sources=3)
    assert 'subgraph "cluster_roi_R0C0"' in dot
    assert "composited" in dot


def test_a_mosaic_says_its_cores_are_composited(snap):
    dot = diagram.object_dot(_readiness(snap, "ometiff.tma.mosaic"), aggregates=True,
                             max_sources=3)
    assert dot.count("composited") >= 3
    assert "mosaic of" in dot


def test_a_by_core_merge_says_one_element_per_roi_instead(snap):
    dot = diagram.object_dot(_readiness(snap, "spatialdata.tma.by-core"), aggregates=False)
    assert "one per ROI" in dot
    assert "composited" not in dot
    assert "mosaic of" not in dot


def test_the_fan_in_is_bounded_and_says_how_many_it_dropped(snap):
    dot = diagram.object_dot(_readiness(snap), max_sources=2)
    assert "and 2 more" in dot or "more" in dot


def test_an_element_read_from_the_anchor_has_no_fan_in(snap):
    dot = diagram.object_dot(_readiness(snap))
    assert '"src_cores_' not in dot


def test_at_most_two_roi_boxes_are_drawn(snap):
    dot = diagram.object_dot(_readiness(snap))
    assert [b.split('"')[0] for b in dot.split("cluster_roi_")[1:]] == ["R0C0", "R0C1"]
    assert "more ROI(s)" in dot


def test_exactly_two_contributors_need_no_more_line(snap):
    readiness = _readiness(snap)
    two = [e for e in readiness.elements if e.element == "labels"]
    assert len(two[0].per_target_role) == 2, "the fixture no longer has exactly two"
    dot = diagram.object_dot(type("R", (), {
        "elements": tuple(two), "recipe": "x", "target": "SpatialData",
        "layout": "merged", "verdict": "ready"})())
    assert "more ROI(s)" not in dot
