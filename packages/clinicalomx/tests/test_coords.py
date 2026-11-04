"""Tests for placement: every path refuses to guess, and a missing scale factor raises."""
from __future__ import annotations

import pytest

from clinicalomx.extract.builders.geo import Frame
from clinicalomx.extract.coords import (
    PAD_RATIO,
    BboxPlacement,
    GeometryPlacement,
    Placement,
    PlacementError,
    crop_box,
)

BBOX = {"bbox_x1": 10.0, "bbox_y1": 20.0, "bbox_x2": 30.0, "bbox_y2": 50.0}


# The crop box - the one function that has to match the bgnorm stage exactly.
# crop_box reproduces bgnorm's padded box exactly.
def test_the_pad_is_computed_from_the_longest_edge_not_each_edge():
    x0, y0, x1, y1 = crop_box((0, 0, 100, 20), pad_ratio=1.1)
    assert (x1 - x0) - 100 == pytest.approx((y1 - y0) - 20, abs=1)
    assert (y1 - y0) - 20 >= 10, "the short axis is padded by the LONG edge's margin"


def test_the_box_reproduces_the_pipelines_float_error_rather_than_correcting_it():
    assert crop_box((0, 0, 100, 20), pad_ratio=1.1) == (-6, -6, 105, 26)


def test_the_box_floors_starts_and_ceils_stops():
    assert crop_box((0.6, 0.6, 10.2, 10.2), pad_ratio=1.0) == (0, 0, 11, 11)


def test_the_box_clamps_to_the_image_when_it_knows_the_size():
    assert crop_box((-50, -50, 500, 500), pad_ratio=1.0, width=100, height=80) == (0, 0, 100, 80)


def test_a_pad_ratio_of_one_adds_nothing():
    assert crop_box((10, 20, 30, 50), pad_ratio=1.0) == (10, 20, 30, 50)


def test_the_default_pad_ratio_is_the_bgnorm_stages():
    assert PAD_RATIO == 1.1


# GeometryPlacement - the only placement that works for a Core.
class _Row:
    """A geometry parquet row, as `geometry_row` returns it."""

    def __init__(self, geom):
        self.geometry = geom

    def get(self, _key, default=None):
        return default


class _Geom:
    def __init__(self, bounds, empty=False):
        self.bounds, self.is_empty = bounds, empty


FULLRES = {"frame": Frame(1.0, "roi_attrs")}
CORE_VALUES = {"source_artifact": "det/grid.parquet", "parquet_row": 3}


def test_a_core_is_placed_from_its_parquet_geometry():
    got = GeometryPlacement().bounds(
        values=CORE_VALUES, geometry_row=_Row(_Geom((10, 20, 30, 50))), container_attrs=FULLRES)
    assert got == (10.0, 20.0, 30.0, 50.0)


def test_detection_frame_geometry_is_scaled_up_to_full_resolution():
    got = GeometryPlacement().bounds(
        values=CORE_VALUES, geometry_row=_Row(_Geom((10, 20, 30, 50))),
        container_attrs={"frame": Frame(16.0, "croi:coordinate_space")})
    assert got == (160.0, 320.0, 480.0, 800.0)


def test_an_unknown_frame_refuses_rather_than_assuming_full_resolution():
    with pytest.raises(PlacementError, match="frame .* is unknown"):
        GeometryPlacement().bounds(
            values=CORE_VALUES, geometry_row=_Row(_Geom((10, 20, 30, 50))), container_attrs={})


def test_an_inferred_grid_cell_is_refused_with_the_right_reason():
    with pytest.raises(PlacementError, match="inferred by the dearray fit"):
        GeometryPlacement().bounds(
            values={**CORE_VALUES, "is_detected": False},
            geometry_row=_Row(_Geom((10, 20, 30, 50))), container_attrs=FULLRES)


def test_an_empty_geometry_is_refused():
    with pytest.raises(PlacementError, match="empty"):
        GeometryPlacement().bounds(
            values=CORE_VALUES, geometry_row=_Row(_Geom((0, 0, 0, 0), empty=True)),
            container_attrs=FULLRES)


def test_a_missing_geometry_row_names_what_is_missing():
    with pytest.raises(PlacementError, match="no geometry row"):
        GeometryPlacement().bounds(values=CORE_VALUES, container_attrs=FULLRES)


def test_geometry_placement_declares_what_the_planner_should_presence_check():
    assert set(GeometryPlacement.NEEDS) == {"source_artifact", "parquet_row"}


def test_geometry_placement_satisfies_the_protocol():
    assert isinstance(GeometryPlacement(), Placement)


def test_bbox_placement_satisfies_the_protocol():
    assert isinstance(BboxPlacement(scale=1.0), Placement)


def test_bbox_placement_takes_the_frame_when_one_was_read():
    got = BboxPlacement().bounds(values=BBOX, container_attrs={"frame": Frame(2.0, "roi_attrs")})
    assert got == (20.0, 40.0, 60.0, 100.0)


def test_bounds_are_scaled_from_detection_pixels_to_full_resolution():
    assert BboxPlacement(scale=4.0).bounds(values=BBOX) == (40.0, 80.0, 120.0, 200.0)


def test_a_missing_scale_factor_raises_rather_than_defaulting_to_one():
    with pytest.raises(PlacementError, match="scale_factor"):
        BboxPlacement().bounds(values=BBOX)


def test_the_scale_error_explains_where_the_factor_went():
    with pytest.raises(PlacementError) as exc:
        BboxPlacement().bounds(values=BBOX)
    message = str(exc.value)
    assert "to_parquet" in message and "Refusing to assume 1.0" in message


def test_a_bare_scale_factor_key_is_no_longer_a_source():
    with pytest.raises(PlacementError, match="scale_factor"):
        BboxPlacement().bounds(values=BBOX, container_attrs={"scale_factor": 2.0})


def test_an_explicit_scale_beats_the_container():
    got = BboxPlacement(scale=1.0).bounds(values=BBOX, container_attrs={"scale_factor": 8.0})
    assert got == (10.0, 20.0, 30.0, 50.0)


def test_the_undescribed_scale_feature_is_no_longer_a_source():
    with pytest.raises(PlacementError, match="Refusing to assume 1.0"):
        BboxPlacement().bounds(values={**BBOX, "scale": "20"})


@pytest.mark.parametrize("bad", [0, -1, "not a number", None, ""],
                         ids=["zero", "negative", "text", "none", "empty"])
def test_an_unusable_scale_factor_is_ignored_rather_than_used(bad):
    with pytest.raises(PlacementError, match="scale_factor"):
        BboxPlacement().bounds(values=BBOX, container_attrs={"scale_factor": bad})


def test_a_missing_bbox_column_says_a_core_needs_the_other_placement():
    with pytest.raises(PlacementError, match="GeometryPlacement"):
        BboxPlacement(scale=1.0).bounds(values={k: v for k, v in BBOX.items() if k != "bbox_x2"})


def test_degenerate_bounds_are_caught_before_anything_is_cropped():
    flat = {**BBOX, "bbox_y2": BBOX["bbox_y1"]}
    with pytest.raises(PlacementError, match="degenerate"):
        BboxPlacement(scale=1.0).bounds(values=flat)


def test_the_coordinate_space_is_declared_and_overridable():
    assert BboxPlacement().coordinate_space == "global"
    assert BboxPlacement(coordinate_space="slide").coordinate_space == "slide"


def test_the_needed_columns_are_declared_for_the_planner():
    assert set(BboxPlacement.NEEDS) <= set(BBOX)
