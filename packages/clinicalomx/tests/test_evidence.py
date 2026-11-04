"""Tests for placement from evidence: a bgnormed zarr's own coords record its crop origin, which
outranks metadata placements; a disagreement is reported."""
from __future__ import annotations

import pytest

pytest.importorskip("xarray", reason="the builders need clinicalomx[extract]")

import dask.array as da  # noqa: E402
import xarray as xr  # noqa: E402

from clinicalomx.extract.builders.imaging import open_image, reset_cache  # noqa: E402
from clinicalomx.extract.coords import (  # noqa: E402
    EVIDENCE_TOLERANCE_PX,
    PlacementError,
    cross_check,
)

PX_UM = 0.5
ORIGIN_PX = (2100, 1050)


@pytest.fixture
def cropped(tmp_path):
    """A zarr whose spatial coords say it was cropped from (2100, 1050) at 0.5 um/px."""
    path = tmp_path / "bgnormed.zarr"
    h, w = 200, 300
    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"), da.zeros((2, h, w), chunks=(1, 64, 64)))},
        coords={
            "c": ["A", "B"],
            "y": [(ORIGIN_PX[1] + i) * PX_UM for i in range(h)],
            "x": [(ORIGIN_PX[0] + i) * PX_UM for i in range(w)],
        },
    )
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": PX_UM}
    ds.to_zarr(path, mode="w")
    reset_cache()
    return path


# The image records where it came from.
def test_a_cropped_image_records_its_own_origin(cropped):
    origin = open_image(cropped, variable="adjusted_image").origin
    assert origin is not None
    assert (round(origin.x), round(origin.y)) == ORIGIN_PX
    assert (origin.width, origin.height) == (300, 200)


def test_the_recorded_origin_is_in_pixels_not_microns(cropped):
    origin = open_image(cropped, variable="adjusted_image").origin
    assert origin.px_um == PX_UM
    assert origin.x == pytest.approx(ORIGIN_PX[0])


def test_default_integer_indices_are_no_evidence_rather_than_origin_zero(tmp_path):
    path = tmp_path / "bare.zarr"
    ds = xr.Dataset({"adjusted_image": (("c", "y", "x"),
                                        da.zeros((1, 4, 4), chunks=(1, 4, 4)))},
                    coords={"c": ["A"]})
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": PX_UM}
    ds.to_zarr(path, mode="w")
    assert open_image(path, variable="adjusted_image").origin is None


def test_coords_whose_spacing_is_not_the_pixel_size_are_no_evidence(tmp_path):
    path = tmp_path / "indexed.zarr"
    ds = xr.Dataset({"adjusted_image": (("c", "y", "x"),
                                        da.zeros((1, 4, 4), chunks=(1, 4, 4)))},
                    coords={"c": ["A"], "y": [0.0, 1.0, 2.0, 3.0], "x": [0.0, 1.0, 2.0, 3.0]})
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": PX_UM}
    ds.to_zarr(path, mode="w")
    assert open_image(path, variable="adjusted_image").origin is None


def test_a_store_with_no_declared_pixel_size_is_still_evidence(tmp_path):
    path = tmp_path / "nopx.zarr"
    xr.Dataset({"adjusted_image": (("c", "y", "x"), da.zeros((1, 4, 4), chunks=(1, 4, 4)))},
               coords={"c": ["A"],
                       "y": [1000.0, 1000.5, 1001.0, 1001.5],
                       "x": [2000.0, 2000.5, 2001.0, 2001.5]},
               ).to_zarr(path, mode="w")
    origin = open_image(path, variable="adjusted_image").origin
    assert origin is not None
    assert (origin.x, origin.y) == (4000.0, 2000.0), "first / step is the offset in pixels"
    assert origin.px_um == 0.5, "the step is the pixel size, measured rather than declared"


# Cross-checking.
EVIDENCE = (2100.0, 1050.0, 2400.0, 1250.0)


def test_agreement_places_from_the_evidence_and_records_both():
    placed = cross_check(EVIDENCE, {"GeometryPlacement": EVIDENCE})
    assert placed.bounds == EVIDENCE
    assert placed.source == "evidence"
    assert placed.disagreements == ()
    assert "GeometryPlacement" in placed.verdicts


def test_a_disagreement_is_named_rather_than_silently_corrected():
    wrong = (131.0, 65.0, 150.0, 78.0)          # a 16x frame error
    placed = cross_check(EVIDENCE, {"GeometryPlacement": wrong})
    assert placed.bounds == EVIDENCE, "the evidence still wins"
    assert placed.disagreements == ("GeometryPlacement",)
    assert "DISAGREES" in placed.verdicts["GeometryPlacement"]


def test_sub_pixel_differences_are_not_a_disagreement():
    near = tuple(v + EVIDENCE_TOLERANCE_PX / 2 for v in EVIDENCE)
    assert cross_check(EVIDENCE, {"GeometryPlacement": near}).disagreements == ()


def test_a_declined_strategy_is_recorded_with_its_reason():
    placed = cross_check(EVIDENCE, {"BboxPlacement": "bbox_x1 carries no value"})
    assert "declined" in placed.verdicts["BboxPlacement"]
    assert placed.disagreements == ()


def test_without_evidence_the_metadata_places_it():
    placed = cross_check(None, {"GeometryPlacement": EVIDENCE})
    assert placed.bounds == EVIDENCE and placed.source == "GeometryPlacement"


def test_no_evidence_and_no_usable_metadata_refuses():
    with pytest.raises(PlacementError, match="no evidence and no metadata"):
        cross_check(None, {"GeometryPlacement": "declined"})


def test_the_verdicts_are_json_serialisable():
    import json

    json.dumps(cross_check(EVIDENCE, {"GeometryPlacement": EVIDENCE}).to_dict())
