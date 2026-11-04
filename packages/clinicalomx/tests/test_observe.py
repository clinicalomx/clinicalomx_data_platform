"""Tests for observation: Artifact-valued features the instance carries are reported, including
ones no installed package declares, and never used to fill a slot."""
from __future__ import annotations

import pytest

from clinicalomx.extract import Element, Recipe
from clinicalomx.extract.observe import observe_all
from test_snapshot import _loaded, _Rec

CORE = _Rec("uC0", "s:TMA0:R0C0", "Core")

RECIPE = Recipe(
    name="r.one", target="OME-TIFF", roi_types=("Core",), layout="single",
    elements=(Element("image", "image", roles=("bgnormed",),
                      needs_features=("source_artifact",)),),
    builder="m:b", placement="m:p")


def _snap(values, *, installed=(), instance=()):
    snap = _loaded({"Core": [CORE]}, {CORE.uid: values})
    snap._role_features = tuple(installed)
    snap._provenance = ()
    snap._artifact_features = tuple(instance)
    snap._role_artifacts = []
    snap._backrefs = {v: {"roi_uid": "x"} for v in values.values() if isinstance(v, str)}
    return snap


# Discovery: the instance knows more than the installed packages.
def test_an_artifact_no_installed_package_declares_is_still_seen():
    snap = _snap({"segmentations": "seg/a.tif"},
                 installed=(), instance=("segmentations",))
    seen = observe_all(snap, recipes=[RECIPE]).of(CORE).seen
    assert [s.feature for s in seen] == ["segmentations"]
    assert seen[0].declared_locally is False


def test_the_instance_versus_installed_gap_is_reported():
    snap = _snap({"segmentations": "seg/a.tif"}, instance=("segmentations",))
    observed = observe_all(snap, recipes=[RECIPE])
    assert observed.instance_only == ("segmentations",)
    assert any("no vocabulary installed here does" in c for c in observed.caveats())


def test_a_dangling_pointer_is_seen_but_not_registered():
    snap = _snap({"bgnormed": "b/a.zarr"}, installed=("bgnormed",))
    snap._backrefs = {}
    assert observe_all(snap, recipes=[RECIPE]).of(CORE).seen[0].registered is False


def test_a_feature_with_no_value_is_not_seen():
    snap = _snap({}, installed=("bgnormed",), instance=("bgnormed",))
    assert observe_all(snap, recipes=[RECIPE]).of(CORE).seen == ()


# Reporting, not acting.
def test_an_artifact_no_recipe_reads_is_reported_as_unclaimed():
    snap = _snap({"roi_thumbnail": "review/t.png"}, instance=("roi_thumbnail",))
    observed = observe_all(snap, recipes=[RECIPE])
    assert [s.feature for s in observed.of(CORE).unclaimed] == ["roi_thumbnail"]
    assert any("read by no recipe" in c for c in observed.caveats())


def test_the_unclaimed_caveat_says_nothing_will_pick_it_up():
    snap = _snap({"roi_thumbnail": "review/t.png"}, instance=("roi_thumbnail",))
    caveat = next(c for c in observe_all(snap, recipes=[RECIPE]).caveats()
                  if "read by no recipe" in c)
    assert "NOT included in any plan" in caveat
    assert "nothing here will pick it up" in caveat


def test_a_role_a_recipe_reads_is_claimed():
    snap = _snap({"bgnormed": "b/a.zarr"}, installed=("bgnormed",))
    assert observe_all(snap, recipes=[RECIPE]).of(CORE).unclaimed == ()


def test_a_placement_input_counts_as_claimed():
    snap = _snap({"source_artifact": "det/g.parquet"}, instance=("source_artifact",))
    assert observe_all(snap, recipes=[RECIPE]).of(CORE).unclaimed == ()


def test_observation_never_produces_a_slot_assignment():
    snap = _snap({"roi_thumbnail": "review/t.png"}, instance=("roi_thumbnail",))
    as_dict = observe_all(snap, recipes=[RECIPE]).of(CORE).to_dict()
    assert set(as_dict["seen"][0]) == {
        "feature", "keys", "registered", "kind", "claimed", "declared_locally"}
    assert "element" not in as_dict and "slot" not in as_dict


def test_classification_never_opens_a_file():
    snap = _snap({"source_artifact": "det/g.parquet"}, instance=("source_artifact",))
    assert observe_all(snap, recipes=[RECIPE]).of(CORE).seen[0].kind is None


def test_the_result_is_json_serialisable():
    import json

    snap = _snap({"bgnormed": "b/a.zarr"}, installed=("bgnormed",))
    json.dumps(observe_all(snap, recipes=[RECIPE]).of(CORE).to_dict())


# It reaches describe.
def test_describe_carries_the_observation_and_its_caveats():
    from clinicalomx import governance
    from clinicalomx.extract.availability import describe

    snap = _snap({"roi_thumbnail": "review/t.png", "bgnormed": "b/a.zarr"},
                 installed=("bgnormed",), instance=("bgnormed", "roi_thumbnail"))
    snap.artifact_rows = lambda keys, columns: {k: {"roi_uid": "x"} for k in keys}
    original = governance.permits_read
    governance.permits_read = lambda key: True
    try:
        av = describe(CORE, snapshot=snap, recipes=[RECIPE], now="2026-01-01T00:00:00Z")
    finally:
        governance.permits_read = original
    assert av.observation is not None
    assert any("no recipe reads" in c for c in av.caveats())


@pytest.mark.parametrize("roi_types", [None, ["Core"]])
def test_observe_is_bulk_and_indexed_by_uid(roi_types):
    snap = _snap({"bgnormed": "b/a.zarr"}, installed=("bgnormed",))
    observed = observe_all(snap, roi_types=roi_types, recipes=[RECIPE])
    assert set(observed.by_uid) == {CORE.uid}
