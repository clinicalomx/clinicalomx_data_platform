"""Tests for slide-anchored products: a core resolves to the same slide-pixel bounds through its
TMA or its slide, and two TMAs' ``R0C0`` never share a well."""
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("zarr", reason="the builders need clinicalomx[extract]")
pytest.importorskip("xarray", reason="the builders need clinicalomx[extract]")

import dask.array as da  # noqa: E402
import tifffile  # noqa: E402
import xarray as xr  # noqa: E402
import zarr  # noqa: E402

from clinicalomx.extract.builders import ngff, ometiff  # noqa: E402
from clinicalomx.extract.builders.imaging import reset_cache  # noqa: E402
from clinicalomx.extract.coords import PlacementSet, crop_box  # noqa: E402

CH = 3
INNER = 88
PITCH = 140
GRID_W = 3
PX_UM = 0.5

#: Where each TMA's grid starts on the slide, in full-resolution slide pixels.
TMAS = {"TMA0": (300, 500), "TMA1": (300, 1300)}

#: A slide-scale offset, where `translation / scale` precision is asserted.
SLIDE_SCALE = {"TMA0": (2_100_000_000, 1_900_000_003), "TMA1": (2_100_000_007, 1_900_050_000)}


def _rc(i):
    return i // GRID_W, i % GRID_W


def _core_name(tma, i):
    r, c = _rc(i)
    return f"slide:{tma}:R{r}C{c}"


def _bounds(origins, tma, i):
    r, c = _rc(i)
    x, y = origins[tma]
    x, y = x + c * PITCH, y + r * PITCH
    return (float(x), float(y), float(x + INNER), float(y + INNER))


def _value(tma, i):
    """A value unique to one core of one TMA."""
    return float(1000 * (1 + list(TMAS).index(tma)) + i)


def _bgnormed_core(path, origins, tma, i):
    """One core's bgnormed zarr: core-sized, at the padded box, with micron coords and no
    `image_info`."""
    x0, y0, x1, y1 = crop_box(_bounds(origins, tma, i))
    h, w = y1 - y0, x1 - x0
    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"),
                            da.full((CH, h, w), _value(tma, i), chunks=(1, 32, 32), dtype="f8"))},
        coords={"c": [f"CD{j}" for j in range(CH)],
                "y": (np.arange(h) + y0) * PX_UM,
                "x": (np.arange(w) + x0) * PX_UM})
    ds.to_zarr(path, mode="w")
    return path


class _Element:
    def __init__(self, name, variable=None):
        self.name, self.kind, self.variable = name, "image", variable


class _Placement:
    coordinate_space = "global"

    def bounds(self, *, values, geometry_row=None, container_attrs=None):
        return values["box"]


class _Type:
    def __init__(self, name):
        self.name = name


class _Child:
    def __init__(self, tma, i):
        self.type = _Type("Core")
        self.uid, self.name = f"u{tma}{i}", _core_name(tma, i)


class _Recipe:
    """Stands in for the slide plate and mosaic recipes: images only, over every core."""

    def __init__(self, cores, elements=("bgnorm",)):
        self.elements = tuple(_Element(n, "adjusted_image" if n != "raw" else None)
                              for n in elements)
        self._children = [_Child(tma, i) for tma, i in cores]

    def targets_under(self, _roi, _snapshot):
        return list(self._children)


class _Unit:
    def __init__(self, output, anchor, cores, elements):
        self.output, self.roi_name, self.roi_uid = str(output), anchor, "uSlide"
        self.elements = {n: ("bgnormed" if n != "raw" else "source_image") for n in elements}
        self.child_elements = {_core_name(t, i): dict(self.elements) for t, i in cores}


class _Snapshot:
    def record_by_uid(self, _uid):
        return "slide"


@pytest.fixture
def build(tmp_path, monkeypatch):
    """One slide's cores on disk. Only the two lamindb lookups are stubbed."""
    def _make(builder, *, anchor="slide", origins=None, cores=None, elements=("bgnorm",),
              placement=None, out="slide", **kw):
        origins = origins or TMAS
        cores = list(cores if cores is not None
                     else [(t, i) for t in origins for i in range(6)])
        zarrs = {_core_name(t, i): _bgnormed_core(tmp_path / f"{t}_{i}.zarr", origins, t, i)
                 for t, i in cores}
        raw = None
        if "raw" in elements:
            span = crop_box(_bounds(origins, list(origins)[-1], 5))
            raw = tmp_path / "slide.tiff"
            tifffile.imwrite(raw, np.zeros((CH, span[3] + 8, span[2] + 8), dtype="uint16"),
                             photometric="minisblack")

        def _resolve(stub, _snapshot):
            tma, rc = stub.roi_name.split(":")[1:3]
            i = next(i for t, i in cores if t == tma and _core_name(t, i).endswith(rc))
            r, c = _rc(i)
            return (stub.roi_name, {"roi_id": rc, "box": _bounds(origins, tma, i)},
                    {"row": r, "col": c}, None)

        monkeypatch.setattr(ometiff, "_resolve", _resolve)
        monkeypatch.setattr(ometiff, "_artifact_for", lambda roi, role, *_a, **_k: (
            zarrs.get(roi) if role == "bgnormed" else raw))
        reset_cache()
        suffix = ".ome.zarr" if builder is ngff.build_slide_plate else ".ome.tiff"
        target = tmp_path / f"{out}{suffix}"
        detail = builder(_Unit(target, anchor, cores, elements),
                         recipe=_Recipe(cores, elements),
                         placement=placement or _Placement(), snapshot=_Snapshot(), **kw)
        return target, detail
    return _make


def _attrs(path):
    return json.loads((path / "zarr.json").read_text())["attributes"]["ome"]


def _recovered_offset(path):
    """``(y, x)`` in full-resolution slide pixels, back out of the written transformation."""
    ct = _attrs(path)["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    scale = next(t for t in ct if t["type"] == "scale")["scale"]
    trans = next(t for t in ct if t["type"] == "translation")["translation"]
    return trans[1] / scale[1], trans[2] / scale[2]


# The composition.
def test_a_core_lands_at_the_same_slide_pixel_whichever_anchor_reached_it(build):
    slide, _d = build(ngff.build_slide_plate, out="whole")
    per_tma = {tma: build(ngff.build_tma_plate, anchor=f"slide:{tma}",
                          cores=[(tma, i) for i in range(6)], out=tma)[0]
               for tma in TMAS}
    for tma in TMAS:
        for i in range(6):
            r, c = _rc(i)
            here = _recovered_offset(slide / f"{tma}R{r}/{c}/0")
            there = _recovered_offset(per_tma[tma] / f"{r}/{c}/0")
            assert here == there, f"{tma} R{r}C{c} moved between anchors"


def test_the_recovered_offset_is_the_pixel_the_mosaic_would_paste_at(build):
    slide, _d = build(ngff.build_slide_plate)
    for tma in TMAS:
        for i in range(6):
            r, c = _rc(i)
            x0, y0, _x1, _y1 = crop_box(_bounds(TMAS, tma, i))
            back_y, back_x = _recovered_offset(slide / f"{tma}R{r}/{c}/0")
            assert round(back_y) == y0 and round(back_x) == x0, f"{tma} R{r}C{c} misplaced"
            assert abs(back_y - y0) < 1e-6 and abs(back_x - x0) < 1e-6


def test_the_composition_is_exact_at_the_offsets_a_real_slide_carries(build):
    slide, _d = build(ngff.build_slide_plate, origins=SLIDE_SCALE, out="far")
    for tma in SLIDE_SCALE:
        for i in range(6):
            r, c = _rc(i)
            x0, y0, _x1, _y1 = crop_box(_bounds(SLIDE_SCALE, tma, i))
            back_y, back_x = _recovered_offset(slide / f"{tma}R{r}/{c}/0")
            assert round(back_y) == y0 and round(back_x) == x0
            assert abs(back_y - y0) < 1e-6 and abs(back_x - x0) < 1e-6


def test_the_slide_mosaic_pastes_every_tma_at_its_own_offset(build):
    out, detail = build(ometiff.build_tma_mosaic, out="mosaic")
    with tifffile.TiffFile(out) as tif:
        canvas = tif.series[0].asarray()
    boxes = [crop_box(_bounds(TMAS, t, i)) for t in TMAS for i in range(6)]
    ox, oy = min(b[0] for b in boxes), min(b[1] for b in boxes)
    assert f"origin ({ox}, {oy})" in detail
    for tma in TMAS:
        for i in range(6):
            x0, y0, _x1, _y1 = crop_box(_bounds(TMAS, tma, i))
            assert canvas[0, y0 - oy + 4, x0 - ox + 4] == _value(tma, i), (
                f"{tma} core {i} is not at its own slide offset")


def test_the_slide_mosaic_canvas_spans_both_tmas(build):
    out, _detail = build(ometiff.build_tma_mosaic, out="span")
    with tifffile.TiffFile(out) as tif:
        canvas = tif.series[0].asarray()
    boxes = [crop_box(_bounds(TMAS, t, i)) for t in TMAS for i in range(6)]
    assert canvas.shape[1] >= max(b[3] for b in boxes) - min(b[1] for b in boxes)
    assert set(np.unique(canvas)) >= {_value(t, i) for t in TMAS for i in range(6)}


# Two TMAs on one slide, which is what distinguishes this from a TMA plate.
def test_two_tmas_do_not_share_a_well(build):
    out, detail = build(ngff.build_slide_plate)
    spec = _attrs(out)["plate"]
    assert [r["name"] for r in spec["rows"]] == ["TMA0R0", "TMA0R1", "TMA1R0", "TMA1R1"]
    assert [c["name"] for c in spec["columns"]] == ["0", "1", "2"]
    assert len(spec["wells"]) == 12
    assert "already taken" not in detail


def test_the_well_index_addresses_the_row_it_names(build):
    out, _detail = build(ngff.build_slide_plate)
    spec = _attrs(out)["plate"]
    rows = [r["name"] for r in spec["rows"]]
    columns = [c["name"] for c in spec["columns"]]
    for well in spec["wells"]:
        row, column = well["path"].split("/")
        assert rows[well["rowIndex"]] == row
        assert columns[well["columnIndex"]] == column
        assert (out / well["path"] / "zarr.json").exists()


def test_each_core_keeps_its_own_pixels_in_its_own_well(build):
    out, _detail = build(ngff.build_slide_plate)
    root = zarr.open_group(out, mode="r")
    for tma in TMAS:
        for i in range(6):
            r, c = _rc(i)
            assert float(root[f"{tma}R{r}/{c}/0/0"][0, 4, 4]) == _value(tma, i)


def test_a_slide_carrying_one_tma_still_names_its_rows_by_it(build):
    out, detail = build(ngff.build_slide_plate, cores=[("TMA0", i) for i in range(6)])
    assert [r["name"] for r in _attrs(out)["plate"]["rows"]] == ["TMA0R0", "TMA0R1"]
    assert "TMA0 rows TMA0R0-TMA0R1" in detail


def test_the_tma_plate_rows_are_unchanged(build):
    out, detail = build(ngff.build_tma_plate, anchor="slide:TMA0",
                        cores=[("TMA0", i) for i in range(6)])
    assert [r["name"] for r in _attrs(out)["plate"]["rows"]] == ["0", "1"]
    assert [w["path"] for w in _attrs(out)["plate"]["wells"]][0] == "0/0"
    assert "rows " not in detail, "a TMA plate has no bands to report"


def test_a_row_label_is_alphanumeric_and_never_collides():
    assert ngff.row_label(("TMA_1",)) == "TMA1"
    assert ngff.row_label(("TMA0", "sub")) == "TMA0sub"
    assert ngff.row_label(("::",)) == "x"
    assert ngff.row_label(()) == "x"


def test_two_routes_that_sanitise_alike_get_separate_bands(build):
    cores, skipped = [], []
    for route in (("TMA_0",), ("TMA.0",)):
        for i in range(2):
            cores.append(ometiff._Target(f"R0C{i}", (0, 0, 1, 1), {}, (0, i), route))
    cells, rows, columns, bands = ngff._grid(cores, skipped, qualify=True)
    assert rows == ["TMA0R0", "TMA0x2R0"], rows
    assert len(cells) == 4 and not skipped
    assert [label for label, _band in bands] == ["TMA0", "TMA0x2"], (
        "the detail line must name the band as the file spells it, not as the route did")


# The declared-placement discipline, which a slide-level fan-out must not weaken.
def test_a_subtype_with_no_declared_placement_is_skipped_and_named(build):
    only_tma = PlacementSet({"TMA": _Placement()})
    with pytest.raises(ValueError) as exc:
        build(ngff.build_slide_plate, placement=only_tma)
    assert "no placement declared for this subtype" in str(exc.value)
    assert "Core" in str(exc.value)


def test_a_declared_subtype_is_routed_through_its_own_placement(build):
    declared = PlacementSet({"Core": _Placement()})
    out, detail = build(ngff.build_slide_plate, placement=declared, out="declared")
    assert len(_attrs(out)["plate"]["wells"]) == 12
    assert "no placement declared" not in detail


# The fan-out, against the real recipes and a real two-TMA spine.
@pytest.fixture
def demo_slide():
    from clinicalomx.extract.app import demo

    demo.install_governance()
    snap = demo.build_snapshot()
    return snap, snap.record_by_name("WholeSlide", demo.SLIDE_NAME)


@pytest.mark.parametrize("name", ["ometiff.slide.mosaic", "omezarr.slide.plate",
                                  "ometiff.slide.per-core"])
def test_a_slide_pixel_recipe_fans_out_over_cores_and_nothing_else(demo_slide, name):
    from clinicalomx.extract import all_recipes
    from clinicalomx.snapshot import type_name_of

    snap, slide = demo_slide
    recipe = next(r for r in all_recipes() if r.name == name)
    targets = recipe.targets_under(slide, snap)
    assert targets, "the demo slide carries cores; the recipe found none"
    assert {type_name_of(t) for t in targets} == {"Core"}
    assert len({str(t.name).split(":")[1] for t in targets}) == 2, "only one TMA was reached"


def test_the_slide_recipes_reach_every_core_the_tma_recipes_do(demo_slide):
    from clinicalomx.extract.recipes import OMEZARR_SLIDE_PLATE, OMEZARR_TMA_PLATE
    from clinicalomx.snapshot import type_name_of

    snap, slide = demo_slide
    tmas = [c for c in snap.children(slide) if type_name_of(c) == "TMA"]
    per_tma = {t.uid for tma in tmas for t in OMEZARR_TMA_PLATE.targets_under(tma, snap)}
    assert {t.uid for t in OMEZARR_SLIDE_PLATE.targets_under(slide, snap)} == per_tma


def test_the_route_separates_two_tmas_that_share_a_core_name(demo_slide):
    from clinicalomx.extract.keys import path_from
    from clinicalomx.extract.recipes import OMEZARR_SLIDE_PLATE

    snap, slide = demo_slide
    shared = [c for c in OMEZARR_SLIDE_PLATE.targets_under(slide, snap)
              if str(c.name).endswith(":R0C0")]
    assert len(shared) == 2, "the demo slide no longer has one core name under two TMAs"
    labels = {ngff.row_label(path_from(str(slide.name), str(c.name))) for c in shared}
    assert labels == {"TMA0", "TMA1"}
