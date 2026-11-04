"""Tests for ``geometry_row``: match on ``roi_id``, fall back to position, raise rather than guess.
Uses real pandas."""
from __future__ import annotations

import pytest

from clinicalomx import geometry

pd = pytest.importorskip("pandas")


def _frame(ids=None, n=3):
    data = {"geometry": [f"g{i}" for i in range(n)], "row": list(range(n)), "col": [0] * n}
    if ids is not None:
        data["roi_id"] = list(ids)
    return pd.DataFrame(data)


def test_the_column_is_the_reserved_feature_name():
    from clinicalomx import FEATURES

    assert geometry.ROI_ID_COLUMN == FEATURES.ROI_ID == "roi_id"


def test_roi_id_for_takes_the_name_s_last_segment():
    assert geometry.roi_id_for("slide:TMA0:R4C2") == "R4C2"
    assert geometry.roi_id_for("slide:TMA0") == "TMA0"
    assert geometry.roi_id_for("slide") == "slide"


def test_matches_on_roi_id_and_ignores_a_stale_position():
    got = geometry.geometry_row(
        _frame(ids=["R0C0", "R1C0", "R2C0"]), roi_id="R2C0", parquet_row=0)
    assert got["geometry"] == "g2"


def test_falls_back_to_position_when_the_column_is_absent():
    got = geometry.geometry_row(_frame(ids=None), roi_id="R1C0", parquet_row=1)
    assert got["geometry"] == "g1"


def test_falls_back_to_position_when_the_column_lacks_this_child():
    got = geometry.geometry_row(
        _frame(ids=["R0C0", "R1C0", "R2C0"]), roi_id="R9C9", parquet_row=2)
    assert got["geometry"] == "g2"


def test_position_only_lookup_still_works():
    got = geometry.geometry_row(_frame(ids=["R0C0", "R1C0", "R2C0"]), roi_id=None, parquet_row=1)
    assert got["geometry"] == "g1"


def test_raises_rather_than_guessing_when_nothing_resolves():
    with pytest.raises(LookupError):
        geometry.geometry_row(_frame(ids=None), roi_id="R1C0", parquet_row=None)


def test_raises_on_an_out_of_range_position():
    with pytest.raises(LookupError, match="out of range"):
        geometry.geometry_row(_frame(ids=None), roi_id=None, parquet_row=99)


def test_raises_on_an_ambiguous_roi_id():
    with pytest.raises(LookupError, match="ambiguous"):
        geometry.geometry_row(_frame(ids=["R0C0", "R0C0", "R2C0"]), roi_id="R0C0", parquet_row=0)
