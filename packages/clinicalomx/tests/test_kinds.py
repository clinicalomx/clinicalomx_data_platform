"""Tests for the element-kind rule: a parquet with geometry is `shapes`, one without is `table`.

``None`` means no opinion: an unknown suffix or an unreadable path.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyarrow", reason="the kind rule reads parquet schemas")
pytest.importorskip("geopandas", reason="the shapes fixtures need the extract extra")

import geopandas as gpd  # noqa: E402
import pandas as pd  # noqa: E402
from shapely.geometry import box  # noqa: E402

from clinicalomx.extract.builders.kinds import (  # noqa: E402
    RULES,
    kind_of_parquet,
    natural_kind,
    resolve_kind,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("cells.h5ad", "table"), ("qc.csv", "table"),
     ("slide.qptiff", "image"), ("crop.tif", "image"), ("crop.tiff", "image"),
     ("overlay.png", "image"),
     # `.zarr` has no kind: SpatialData, AnnData and label images are all zarrs.
     ("bgnormed.zarr", None), ("merged.zarr", None),
     ("notes.txt", None), ("no-suffix", None)],
    ids=lambda v: str(v),
)
def test_the_suffix_settles_it_where_it_can(name, expected):
    assert natural_kind(name) == expected


def test_a_variable_named_labels_is_a_label_image_whatever_the_container():
    assert natural_kind("bgnormed.zarr", variable="labels") == "labels"
    assert natural_kind("seg.zarr", variable="segmentation") == "labels"
    assert natural_kind("anything.tif", variable="mask") == "labels"


def test_a_zarr_variable_that_is_not_a_label_gets_no_opinion():
    assert natural_kind("bgnormed.zarr", variable="adjusted_image") is None


def test_a_parquet_with_geometry_is_shapes(tmp_path):
    path = tmp_path / "cores.parquet"
    gpd.GeoDataFrame({"roi_id": ["R0C0"]}, geometry=[box(0, 0, 1, 1)]).to_parquet(path)
    assert kind_of_parquet(path) == "shapes"
    assert resolve_kind(path) == "shapes"


def test_a_parquet_without_geometry_is_a_table(tmp_path):
    path = tmp_path / "bgnorm-qc.parquet"
    pd.DataFrame({"channel": ["CD8"], "cohens_d": [1.2], "bgnorm_failed": [False]}).to_parquet(path)
    assert kind_of_parquet(path) == "table"
    assert resolve_kind(path) == "table"


def test_an_unreadable_parquet_is_no_opinion_rather_than_a_finding(tmp_path):
    path = tmp_path / "not-really.parquet"
    path.write_text("this is not a parquet")
    assert kind_of_parquet(path) is None


def test_the_documented_rules_cover_every_kind_the_code_returns():
    documented = " ".join(f"{desc} {kind}" for desc, kind in RULES)
    for kind in ("shapes", "table", "image", "labels"):
        assert kind in documented
    assert "no opinion" in documented, "the zarr abstention is part of the rule, not an omission"
