"""``contexts.accepted_cells`` reproduces core-persist's reviewer-grid rows exactly.

Row and column decide Core identity, and row order decides ``parquet_row``.
"""
from __future__ import annotations

import pytest

from conftest import load_pipeline_module

gpd = pytest.importorskip("geopandas")
shapely_geom = pytest.importorskip("shapely.geometry")
contexts = load_pipeline_module("contexts")

# What the review app writes into roi_corrected_json: crop px, with rc and inferred.
SRC_BOXES = [
    {"class": "full_core", "xyxy": [10, 10, 60, 60], "rc": [0, 0], "inferred": False},
    {"class": "inferred_core", "xyxy": [70, 10, 120, 60], "rc": [0, 1], "inferred": True},
    {"class": "partial_core", "xyxy": [10, 70, 60, 120], "rc": [1, 0], "inferred": False},
]


def _todays_grid():
    """The reference construction, frozen. Do not change it to match a refactor."""
    box = shapely_geom.box
    return gpd.GeoDataFrame(
        [
            {
                "row": int(b["rc"][0]), "col": int(b["rc"][1]),
                "class": b["class"], "conf": 1.0, "detection_idx": i,
                "occupied": not bool(b.get("inferred", False)),
                "is_detected": not bool(b.get("inferred", False)),
                "geometry": box(*b["xyxy"]),
            }
            for i, b in enumerate(SRC_BOXES)
        ],
        geometry="geometry",
    ).reset_index(drop=True)


def _derived_grid():
    box = shapely_geom.box
    space = contexts.Space(name="crop", width=1000, height=900, scale_factor=16.0)
    accepted = contexts.AcceptedSet(
        boxes=[contexts.Box.from_legacy(b) for b in SRC_BOXES], space=space)
    rows = contexts.accepted_cells(accepted)
    return gpd.GeoDataFrame(
        [{**{k: v for k, v in r.items() if k != "xyxy"}, "geometry": box(*r["xyxy"])}
         for r in rows],
        geometry="geometry",
    ).reset_index(drop=True)


def test_columns_match() -> None:
    assert list(_derived_grid().columns) == list(_todays_grid().columns)


@pytest.mark.parametrize("col", ["row", "col", "class", "conf", "detection_idx",
                                 "occupied", "is_detected"])
def test_column_values_match(col: str) -> None:
    assert _derived_grid()[col].tolist() == _todays_grid()[col].tolist()


def test_geometry_and_row_order_match() -> None:
    assert [g.bounds for g in _derived_grid().geometry] == \
           [g.bounds for g in _todays_grid().geometry]


def test_core_names_are_identical() -> None:
    def names(g):
        return [f"R{r}C{c}" for r, c in zip(g["row"], g["col"])]

    assert names(_derived_grid()) == names(_todays_grid()) == ["R0C0", "R0C1", "R1C0"]


def test_ungridded_box_does_not_silently_become_r0c0() -> None:
    space = contexts.Space(name="crop", width=100, height=100)
    accepted = contexts.AcceptedSet(
        boxes=[contexts.Box(cls="full_core", xyxy=(0, 0, 1, 1))], space=space)
    assert not accepted.fully_gridded
    assert contexts.accepted_cells(accepted)[0]["row"] == 0  # documented, not relied upon
