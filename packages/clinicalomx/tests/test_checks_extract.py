"""Tests for the recipe-catalogue checks. `needs_features_are_populated` separates a false
declaration from an ordinary data gap."""
from __future__ import annotations

import pytest

import clinicalomx.checks.extract as chk
from clinicalomx.extract import Element, Recipe
from test_snapshot import _loaded, _Rec

CORES = [_Rec(f"u{i}", f"s:TMA0:R0C{i}", "Core") for i in range(30)]


@pytest.fixture
def snap():
    """Thirty Cores, every one carrying parquet_row and source_artifact, none carrying bbox_x1."""
    vals = {c.uid: {"parquet_row": i, "source_artifact": "det/g.parquet"}
            for i, c in enumerate(CORES)}
    return _loaded({"Core": CORES}, vals)


def _recipe(needs=(), roles=("source_image",), **kw):
    base = {"name": "r.one", "target": "OME-TIFF", "roi_types": ("Core",), "layout": "single",
            "elements": (Element("image", "image", roles=roles, needs_features=needs),),
            "builder": "m:b", "placement": "m:p"}
    return Recipe(**{**base, **kw})


@pytest.fixture(autouse=True)
def _catalogue(monkeypatch):
    def use(recipes):
        monkeypatch.setattr(chk, "_declared_recipes", lambda: list(recipes))
    return use


def test_a_feature_no_record_carries_is_reported_as_a_declaration_fault(snap, _catalogue):
    _catalogue([_recipe(needs=("bbox_x1", "parquet_row"))])
    (finding,) = chk.needs_features_are_populated(snap)
    assert "'bbox_x1'" in finding
    assert "0 of 30" in finding
    assert "DECLARATION fault" in finding


def test_the_message_says_what_to_do_about_it(snap, _catalogue):
    _catalogue([_recipe(needs=("bbox_x1",))])
    (finding,) = chk.needs_features_are_populated(snap)
    assert "Owner:" in finding
    assert "Remove it from needs_features" in finding


def test_a_populated_feature_is_silent(snap, _catalogue):
    _catalogue([_recipe(needs=("parquet_row",))])
    assert chk.needs_features_are_populated(snap) == []


def test_a_partially_populated_feature_is_silent(snap, _catalogue):
    snap._values[CORES[0].uid]["bbox_x1"] = 10.0
    _catalogue([_recipe(needs=("bbox_x1",))])
    assert chk.needs_features_are_populated(snap) == []


def test_a_role_nobody_produces_is_not_a_declaration_fault(snap, _catalogue):
    _catalogue([_recipe(roles=("cell_tables",), needs=())])
    assert chk.needs_features_are_populated(snap) == []


def test_a_small_population_says_nothing(_catalogue):
    small = _loaded({"Core": CORES[:5]}, {c.uid: {} for c in CORES[:5]})
    _catalogue([_recipe(needs=("bbox_x1",))])
    assert chk.needs_features_are_populated(small) == []


def test_an_roi_subtype_with_no_records_is_skipped(_catalogue):
    empty = _loaded({"Core": []}, {})
    _catalogue([_recipe(needs=("bbox_x1",))])
    assert chk.needs_features_are_populated(empty) == []


def test_a_children_level_element_is_checked_against_the_CHILD_subtype(_catalogue):
    tma = _Rec("uT", "s:TMA0", "TMA")
    snap = _loaded({"TMA": [tma], "Core": CORES},
                   {**{c.uid: {} for c in CORES}, "uT": {}})
    _catalogue([Recipe(
        name="r.merged", target="SpatialData", roi_types=("TMA",), layout="merged",
        child_types=("Core",),
        elements=(Element("image", "image", roles=("source_image",), level="children",
                          needs_features=("bbox_x1",)),),
        builder="m:b", placement="m:p")])
    (finding,) = chk.needs_features_are_populated(snap)
    assert "every Core" in finding, "the assertion is about Cores, not the one TMA"


def test_one_shared_element_reports_once_not_once_per_recipe(snap, _catalogue):
    shared = Element("image", "image", roles=("source_image",), needs_features=("bbox_x1",))
    _catalogue([_recipe(name=f"r.{i}") for i in range(4)]
               + [Recipe(name="r.shared", target="OME-TIFF", roi_types=("Core",),
                         layout="single", elements=(shared,), builder="m:b", placement="m:p")])
    assert len(chk.needs_features_are_populated(snap)) == 1


def test_the_check_is_registered_as_an_error_and_needs_no_storage():
    check = next(c for c in chk.extract_checks()
                 if c.name == "extract.needs_features_are_populated")
    assert check.severity == "error"
    assert check.needs_storage is False
