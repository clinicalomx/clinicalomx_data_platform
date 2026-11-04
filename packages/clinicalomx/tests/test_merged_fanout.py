"""Tests for `build_merged` over a mixed spine: an undeclared subtype is skipped and named.

Stubbed at the artifact boundary; these assert the fan-out, not the pixels.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spatialdata", reason="the builders need clinicalomx[extract]")

from clinicalomx.extract.app import demo  # noqa: E402
from clinicalomx.extract.builders import spatialdata as sd  # noqa: E402
from clinicalomx.extract.coords import PlacementSet  # noqa: E402
from clinicalomx.extract.recipes import SPATIALDATA_SLIDE_BY_ROI as RECIPE  # noqa: E402


class _Placer:
    coordinate_space = "global"

    def bounds(self, **_kw):
        return (0.0, 0.0, 8.0, 8.0)


class _Unit:
    def __init__(self, slide, children):
        self.roi_uid, self.roi_name = slide.uid, str(slide.name)
        self.output = "/dev/null/unused.zarr"
        self.elements = {"raw": "source_image"}
        self.child_elements = {str(c.name): {"raw": "source_image"} for c in children}


@pytest.fixture
def built(monkeypatch, tmp_path):
    """Run build_merged over the demo slide, recording what was added and what was skipped."""
    demo.install_governance()
    snap = demo.build_snapshot()
    slide = snap.record_by_name("WholeSlide", demo.SLIDE_NAME)
    children = RECIPE.targets_under(slide, snap)

    added: list[str] = []
    monkeypatch.setattr(sd, "_add", lambda s, kind, key, *a, **k: added.append(key))
    monkeypatch.setattr(sd, "_write", lambda s, out: None)
    monkeypatch.setattr(sd, "_elements_for", lambda unit, recipe, placer, snapshot: {
        "raw": (recipe.element("raw"), object(), (0, 0, 8, 8))})

    unit = _Unit(slide, children)
    unit.output = str(tmp_path / "slide.zarr")
    placements = PlacementSet({t: _Placer() for t in RECIPE.placements})
    detail = sd.build_merged(unit, recipe=RECIPE, placement=placements, snapshot=snap)
    return added, detail


def test_a_subtype_with_no_declared_placement_is_skipped_and_named(built):
    _added, detail = built
    assert "TissueSection0" in detail
    assert "no placement declared" in detail


def test_the_placeable_subtypes_still_build(built):
    added, _detail = built
    assert any(k.startswith("TMA0_Core_R0C") for k in added)
    assert any(k.startswith("TMA_TMA") for k in added), "the TMAs' own images were dropped"


def test_every_element_key_in_one_object_is_unique(built):
    added, _detail = built
    assert len(added) == len(set(added))


def test_the_twin_cores_both_survive(built):
    added, _detail = built
    assert {"TMA0_Core_R0C0_raw", "TMA1_Core_R0C0_raw"} <= set(added)
