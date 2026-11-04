"""GeoParquet IO preserves ``GeoDataFrame.attrs`` across a write and read, on real files."""
from __future__ import annotations

import pytest

from conftest import load_pipeline_module

roi_io = load_pipeline_module("roi_io")

gpd = pytest.importorskip("geopandas")
pytest.importorskip("pyarrow")
from shapely.geometry import box


def _gdf(**attrs):
    g = gpd.GeoDataFrame(
        {"class": ["TMA", "tissue_section"], "conf": [0.9, 0.5]},
        geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)],
        crs=None,
    )
    g.attrs.update(attrs)
    return g


def test_scale_factor_survives_the_round_trip(tmp_path):
    path = tmp_path / "rois.parquet"
    roi_io.write_roi_parquet(_gdf(scale_factor=4.0, thumb_w=800, thumb_h=600), path)
    back = roi_io.read_roi_parquet(path)
    assert back.attrs["scale_factor"] == 4.0
    assert (back.attrs["thumb_w"], back.attrs["thumb_h"]) == (800, 600)


def test_plain_geopandas_would_have_lost_it(tmp_path):
    path = tmp_path / "plain.parquet"
    _gdf(scale_factor=4.0).to_parquet(path)
    assert "scale_factor" not in gpd.read_parquet(path).attrs


def test_the_rows_and_geometry_survive_too(tmp_path):
    path = tmp_path / "rois.parquet"
    original = _gdf(scale_factor=1.0)
    roi_io.write_roi_parquet(original, path)
    back = roi_io.read_roi_parquet(path)
    assert list(back["class"]) == ["TMA", "tissue_section"]
    assert back.geometry.iloc[0].bounds == (0.0, 0.0, 10.0, 10.0)
    assert len(back) == len(original)


def test_no_attrs_is_written_and_read_cleanly(tmp_path):
    path = tmp_path / "bare.parquet"
    roi_io.write_roi_parquet(_gdf(), path)
    assert roi_io.read_roi_parquet(path).attrs == {}


def test_a_str_path_works_like_a_path_object(tmp_path):
    p = tmp_path / "s.parquet"
    roi_io.write_roi_parquet(_gdf(scale_factor=2.5), str(p))
    assert roi_io.read_roi_parquet(str(p)).attrs["scale_factor"] == 2.5


def test_attrs_do_not_leak_between_files(tmp_path):
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    roi_io.write_roi_parquet(_gdf(scale_factor=4.0), a)
    roi_io.write_roi_parquet(_gdf(scale_factor=9.0), b)
    assert roi_io.read_roi_parquet(a).attrs["scale_factor"] == 4.0
    assert roi_io.read_roi_parquet(b).attrs["scale_factor"] == 9.0
