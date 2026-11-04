"""Tests for the OME-NGFF plate builder: wells, registration metadata, pixels and peak memory."""
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("zarr", reason="the NGFF writer needs clinicalomx[extract]")
pytest.importorskip("xarray", reason="the NGFF writer needs clinicalomx[extract]")

import dask.array as da  # noqa: E402
import tifffile  # noqa: E402
import xarray as xr  # noqa: E402
import zarr  # noqa: E402

from clinicalomx.extract.builders import ngff, ometiff  # noqa: E402
from clinicalomx.extract.builders.imaging import reset_cache  # noqa: E402
from clinicalomx.extract.coords import crop_box  # noqa: E402

CH = 3
INNER = 88
PITCH = 140
GRID_W = 3
SLIDE_X, SLIDE_Y = 300, 500
PX_UM = 0.5


def _rc(i):
    return i // GRID_W, i % GRID_W


def _name(i):
    r, c = _rc(i)
    return f"R{r}C{c}"


def _bounds(i):
    """Core ``i``'s unpadded slide box, on a grid away from the slide corner."""
    r, c = _rc(i)
    x, y = SLIDE_X + c * PITCH, SLIDE_Y + r * PITCH
    return (float(x), float(y), float(x + INNER), float(y + INNER))


def _value(base, j):
    """Core ``base``'s channel ``j``. Distinct per channel, so a misplaced plane shows."""
    return float(base + j)


def _stack(base, h, w, dtype):
    return da.stack([da.full((h, w), _value(base, j), chunks=(32, 32), dtype=dtype)
                     for j in range(CH)])


def _bgnormed_core(path, i, *, dtype="f8"):
    """One core's bgnormed zarr as the pipeline writes it: at the padded box, with micron y/x
    coords and no ``image_info``."""
    x0, y0, x1, y1 = crop_box(_bounds(i))
    h, w = y1 - y0, x1 - x0
    coords = {"c": [f"CD{j}" for j in range(CH)],
              "y": (np.arange(h) + y0) * PX_UM,
              "x": (np.arange(w) + x0) * PX_UM}
    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"), _stack(i + 1, h, w, dtype)),
         "adjusted_image_post_hoc": (("c", "y", "x"), _stack(100 + i, h, w, dtype))},
        coords=coords)
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
    name = "Core"


class _Child:
    type = _Type()

    def __init__(self, i):
        self.uid, self.name = f"uC{i}", f"slide:TMA0:{_name(i)}"


class _Recipe:
    elements = (_Element("raw"), _Element("bgnorm", "adjusted_image"),
                _Element("bgnormQ", "adjusted_image_post_hoc"))

    def __init__(self, ids):
        self._children = [_Child(i) for i in ids]

    def targets_under(self, _roi, _snapshot):
        return list(self._children)


class _Unit:
    roi_name, roi_uid = "slide:TMA0", "uid0"

    def __init__(self, output, ids, per_core=None):
        self.output = str(output)
        self.elements = {"raw": "source_image", "bgnorm": "bgnormed", "bgnormQ": "bgnormed"}
        self.child_elements = {_Child(i).name: dict((per_core or {}).get(i, self.elements))
                               for i in ids}


class _Snapshot:
    def record_by_uid(self, _uid):
        return "TMA0"


@pytest.fixture
def plate(tmp_path, monkeypatch):
    """A whole TMA on disk. Only the two lamindb lookups are stubbed."""
    def _make(ids=range(6), *, downsample=1, cores=None, per_core=None,
              use_names=False, collide=False, ungridded=(), out="tma"):
        ids = list(ids)
        have = ids if cores is None else list(cores)
        zarrs = {_name(i): _bgnormed_core(tmp_path / f"c{i}.zarr", i) for i in have}
        raw = tmp_path / "slide.tiff"
        tifffile.imwrite(raw, np.zeros(
            (CH, SLIDE_Y + 4 * PITCH, SLIDE_X + 4 * PITCH), dtype="uint16"),
            photometric="minisblack")

        def _resolve(stub, _snapshot):
            i = [j for j in ids if stub.roi_name.endswith(_name(j))][0]
            r, c = _rc(i)
            if collide:
                r, c = 0, 0
            row = None if (use_names or i in ungridded) else {"row": r, "col": c}
            name = "ZZ" if i in ungridded else _name(i)
            return name, {"roi_id": name, "box": _bounds(i)}, row, None

        monkeypatch.setattr(ometiff, "_resolve", _resolve)
        monkeypatch.setattr(ometiff, "_artifact_for", lambda roi, role, *_a, **_k: (
            zarrs.get(roi) if role == "bgnormed" else raw))
        reset_cache()
        out = tmp_path / f"{out}.ome.zarr"
        unit = _Unit(out, ids, per_core)
        detail = ngff.build_tma_plate(unit, recipe=_Recipe(ids), placement=_Placement(),
                                      snapshot=_Snapshot(), downsample=downsample)
        return out, detail
    return _make


def _attrs(path):
    return json.loads((path / "zarr.json").read_text())["attributes"]["ome"]


# The plate says where every core belongs.
def test_the_plate_declares_every_row_and_column_including_the_empty_ones(plate):
    out, _detail = plate(ids=[0, 1, 2, 5])           # row 1 has only column 2
    spec = _attrs(out)["plate"]
    assert [r["name"] for r in spec["rows"]] == ["0", "1"]
    assert [c["name"] for c in spec["columns"]] == ["0", "1", "2"]
    assert [w["path"] for w in spec["wells"]] == ["0/0", "0/1", "0/2", "1/2"]
    assert {(w["rowIndex"], w["columnIndex"]) for w in spec["wells"]} == {
        (0, 0), (0, 1), (0, 2), (1, 2)}


def test_the_well_path_is_its_own_row_and_column(plate):
    out, _detail = plate()
    for well in _attrs(out)["plate"]["wells"]:
        assert (out / well["path"] / "zarr.json").exists(), f"{well['path']} has no group"
        assert well["path"] == f"{well['rowIndex']}/{well['columnIndex']}"


def test_the_version_is_declared_once_per_group_and_never_inside_the_object(plate):
    out, _detail = plate()
    seen = {_attrs(out)["version"], _attrs(out / "0/0")["version"],
            _attrs(out / "0/0/0")["version"]}
    assert seen == {ngff.NGFF_VERSION}
    assert "version" not in _attrs(out)["plate"]
    assert "version" not in _attrs(out / "0/0")["well"]


def test_a_core_with_no_grid_position_is_skipped_and_named(plate):
    out, detail = plate(ungridded=(4,))
    assert "ZZ" in detail and "no well to put it in" in detail
    assert len(_attrs(out)["plate"]["wells"]) == 5


def test_two_cores_claiming_one_well_keeps_the_first_and_says_so(plate):
    out, detail = plate(ids=[0, 1], collide=True)
    assert "already taken by" in detail
    assert len(_attrs(out)["plate"]["wells"]) == 1


def test_a_well_nothing_could_be_read_for_is_left_out_of_the_plate(plate, monkeypatch):
    real = ngff.crop

    def _unreadable(opened, bounds, **kw):
        if tuple(bounds) == tuple(_bounds(1)):
            raise OSError("the store went away mid-build")
        return real(opened, bounds, **kw)

    monkeypatch.setattr(ngff, "crop", _unreadable)
    out, detail = plate()
    spec = _attrs(out)["plate"]
    assert [w["path"] for w in spec["wells"]] == ["0/0", "0/2", "1/0", "1/1", "1/2"]
    assert not (out / "0/1").exists(), "an empty well must not get a group"
    assert [c["name"] for c in spec["columns"]] == ["0", "1", "2"], "the grid must not shrink"
    assert "R0C1" in detail and "well 0/1" in detail


def test_a_field_that_failed_part_way_through_is_taken_back_out(plate, monkeypatch):
    real, state = ngff.crop, {"n": 0}

    def _fails_mid_field(opened, bounds, **kw):
        if tuple(bounds) == tuple(_bounds(1)):
            state["n"] += 1
            if state["n"] % 2 == 0:       # the first plane of each field lands, the second raises
                raise OSError("the store went away mid-field")
        return real(opened, bounds, **kw)

    monkeypatch.setattr(ngff, "crop", _fails_mid_field)
    out, detail = plate()

    root = zarr.open_group(out, mode="r")
    assert state["n"] > 1, "nothing failed after a field group existed, so `_drop` was never asked"
    assert [w["path"] for w in _attrs(out)["plate"]["wells"]] == ["0/0", "0/2", "1/0", "1/1", "1/2"]
    # The well group is gone too.
    for index in range(3):
        assert not (out / "0/1" / str(index)).exists(), (
            f"field 0/1/{index} was written part way through and left behind")
    assert not (out / "0/1").exists(), "an emptied well must not survive a mid-field failure"
    assert "1" not in set(root["0"].group_keys()), "the emptied well is still in the hierarchy"
    assert set(root["0"].group_keys()) == {"0", "2"}, "the row lost a well that did land"
    assert "R0C1" in detail and "well 0/1" in detail
    for i in (0, 2, 3, 4, 5):
        r, c = _rc(i)
        stack = root[f"{r}/{c}/1/0"][:]
        assert stack.shape[0] == CH
        assert [float(stack[j, 4, 4]) for j in range(CH)] == [
            _value(i + 1, j) for j in range(CH)], f"{_name(i)} lost pixels to a neighbour's failure"


def test_the_field_count_is_what_a_well_actually_holds(plate):
    out, _detail = plate(per_core={i: {"raw": "source_image"} for i in range(6)})
    assert _attrs(out)["plate"]["field_count"] == 1
    out, _detail = plate(per_core={4: {"raw": "source_image"}}, out="mixed")
    assert _attrs(out)["plate"]["field_count"] == 3


def test_the_grid_falls_back_to_the_core_name(plate):
    out, _detail = plate(use_names=True)
    assert {w["path"] for w in _attrs(out)["plate"]["wells"]} == {
        "0/0", "0/1", "0/2", "1/0", "1/1", "1/2"}


def test_grid_of_prefers_the_parquet_over_the_name():
    assert ngff.grid_of({"row": 3, "col": 7}, "R0C0") == (3, 7)
    assert ngff.grid_of(None, "R4C2") == (4, 2)
    assert ngff.grid_of({}, "not-a-grid-name") is None


# Registration.
def test_the_translation_round_trips_to_the_pixel_the_mosaic_would_paste_at(plate):
    out, _detail = plate()
    for i in range(6):
        r, c = _rc(i)
        x0, y0, _x1, _y1 = crop_box(_bounds(i))
        for field in ("0", "1", "2"):
            ct = _attrs(out / f"{r}/{c}/{field}")["multiscales"][0]["datasets"][0][
                "coordinateTransformations"]
            scale = next(t for t in ct if t["type"] == "scale")["scale"]
            trans = next(t for t in ct if t["type"] == "translation")["translation"]
            back_y, back_x = trans[1] / scale[1], trans[2] / scale[2]
            assert round(back_y) == y0 and round(back_x) == x0, f"{_name(i)}/{field} misplaced"
            assert abs(back_y - y0) < 1e-6 and abs(back_x - x0) < 1e-6


def test_the_translation_round_trip_holds_at_any_offset_a_slide_can_carry(plate):
    for offset in (2_100_000_000, 2_100_000_001, 1_234_567_891):
        ct = ngff.transforms((offset, offset + 7), px_um=0.325)
        scale = next(t for t in ct if t["type"] == "scale")["scale"]
        trans = next(t for t in ct if t["type"] == "translation")["translation"]
        back_y, back_x = trans[1] / scale[1], trans[2] / scale[2]
        assert abs(back_y - offset) < 1e-6 and abs(back_x - (offset + 7)) < 1e-6
        assert round(back_y) == offset and round(back_x) == offset + 7


def test_the_transformations_are_shaped_the_way_the_spec_requires(plate):
    out, _detail = plate()
    meta = _attrs(out / "0/0/1")["multiscales"][0]
    ct = meta["datasets"][0]["coordinateTransformations"]
    assert [t["type"] for t in ct] == ["scale", "translation"]
    assert len(ct[0]["scale"]) == len(ct[1]["translation"]) == len(meta["axes"]) == 3
    assert [a["name"] for a in meta["axes"]] == ["c", "y", "x"]
    assert [a["type"] for a in meta["axes"]] == ["channel", "space", "space"]


def test_the_whole_plate_shares_one_pixel_size(plate):
    out, detail = plate()
    scales = {tuple(_attrs(out / f"{r}/{c}/{f}")["multiscales"][0]["datasets"][0][
                    "coordinateTransformations"][0]["scale"])
              for r, c in ((0, 0), (1, 2)) for f in ("0", "1", "2")}
    assert scales == {(1.0, PX_UM, PX_UM)}, "the plate holds more than one frame"
    assert f"{PX_UM} um/px from" in detail, "the detail does not say where the scale came from"


def test_a_plate_where_nothing_recorded_a_pixel_size_stays_in_pixels(plate):
    assert all("unit" not in a for a in ngff.axes(None)), "an unmeasured micron unit was claimed"
    assert ngff.transforms((500, 300), px_um=None) == [
        {"type": "scale", "scale": [1.0, 1.0, 1.0]},
        {"type": "translation", "translation": [0.0, 500.0, 300.0]}]
    assert [a.get("unit") for a in ngff.axes(0.5)] == [None, "micrometer", "micrometer"]


def test_the_plate_pixel_size_is_a_sample_and_names_where_it_came_from(plate):
    out, detail = plate()
    assert "um/px from" in detail
    assert _attrs(out / "0/0/1")["multiscales"][0]["axes"][1]["unit"] == "micrometer"


def test_a_store_that_would_not_open_while_sampling_the_pixel_size_is_named(plate, monkeypatch):
    real, seen = ngff.open_image, []

    def _fails_while_sampling(art, **kw):
        seen.append(art)
        if len(seen) <= 3:                 # one core x three elements: the sampling pass
            raise OSError("the store was busy")
        return real(art, **kw)

    monkeypatch.setattr(ngff, "open_image", _fails_while_sampling)
    out, detail = plate(ids=[0])
    assert "PIXEL SIZE UNREAD" in detail and "the store was busy" in detail
    assert "PIXEL SIZE FOUND TOO LATE" in detail and f"measures {PX_UM} um" in detail, (
        "the fields measured a pixel size while being written and the plate said none did")
    assert "NO store recorded a pixel size" not in detail
    assert _attrs(out / "0/0/1")["multiscales"][0]["datasets"][0][
        "coordinateTransformations"][0]["scale"] == [1.0, 1.0, 1.0]


def test_the_pixel_size_is_measured_from_the_coords_not_from_absent_metadata(plate):
    out, _detail = plate()
    scale = _attrs(out / "0/0/1")["multiscales"][0]["datasets"][0][
        "coordinateTransformations"][0]["scale"]
    assert scale == [1.0, PX_UM, PX_UM]


# The pixels, and which well they land in.
def test_each_core_keeps_its_own_pixels_in_its_own_well(plate):
    out, _detail = plate()
    root = zarr.open_group(out, mode="r")
    for i in range(6):
        r, c = _rc(i)
        for field, base in ((1, i + 1), (2, 100 + i)):
            stack = root[f"{r}/{c}/{field}/0"][:]
            assert stack.shape[0] == CH, f"{_name(i)} field {field} lost a channel"
            assert [float(stack[j, 4, 4]) for j in range(CH)] == [
                _value(base, j) for j in range(CH)], (
                f"{_name(i)} field {field} is not its own, channel for channel")


def test_the_float64_series_are_halved_on_the_way_in(plate):
    out, _detail = plate()
    root = zarr.open_group(out, mode="r")
    assert str(root["0/0/1/0"].dtype) == "float32"
    assert str(root["0/0/0/0"].dtype) == "uint16", "the raw series must not be widened"


def test_a_field_index_means_the_same_thing_in_every_well(plate):
    out, _detail = plate(per_core={4: {"raw": "source_image", "bgnormQ": "bgnormed"}})
    assert [im["path"] for im in _attrs(out / "1/1")["well"]["images"]] == ["0", "2"]
    assert [im["path"] for im in _attrs(out / "0/0")["well"]["images"]] == ["0", "1", "2"]
    assert _attrs(out / "1/1/2")["multiscales"][0]["name"] == "bgnormQ"


def test_the_channels_survive_into_the_plate(plate):
    out, _detail = plate()
    channels = _attrs(out / "0/0/1")["omero"]["channels"]
    assert [ch["label"] for ch in channels] == [f"CD{i}" for i in range(CH)]


class _Meta:
    def __init__(self, colors):
        self.names, self.colors = [f"CD{i}" for i in range(len(colors))], colors


def test_a_channel_colour_is_hex_or_it_is_not_written():
    channels = ngff._omero(_Meta(["0,0,255", "#ff8000", "AABBCC", "not a colour", ""]), 5)[
        "channels"]
    assert [ch.get("color") for ch in channels] == ["0000FF", "FF8000", "AABBCC", None, None]
    assert [ch["label"] for ch in channels] == [f"CD{i}" for i in range(5)], (
        "a bad colour cost the channel labels")


def test_the_plate_reads_back_through_a_plain_zarr_open(plate):
    out, _detail = plate()
    root = zarr.open_group(out, mode="r")
    assert root["0/0/0/0"].shape[0] == CH
    assert root.attrs["ome"]["plate"]["field_count"] == 3


# Downsample is stride decimation.
def test_downsample_is_stride_decimation_and_never_an_average(plate):
    full, _d = plate(out="full")
    small, detail = plate(downsample=2, out="half")
    a = zarr.open_group(full, mode="r")["0/0/1/0"][:]
    b = zarr.open_group(small, mode="r")["0/0/1/0"][:]
    assert b.shape == (CH, -(-a.shape[1] // 2), -(-a.shape[2] // 2))
    assert np.array_equal(b, a[:, ::2, ::2]), "the values are not the sampled ones"
    assert "SAMPLED 2x by stride decimation" in detail


def test_a_decimated_field_widens_the_scale_and_leaves_the_translation_alone(plate):
    out, _detail = plate(downsample=2)
    ct = _attrs(out / "0/0/1")["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    x0, y0, _x1, _y1 = crop_box(_bounds(0))
    assert ct[0]["scale"] == [1.0, PX_UM * 2, PX_UM * 2]
    assert ct[1]["translation"] == [0.0, y0 * PX_UM, x0 * PX_UM]


# Peak memory is one plane.
def test_the_plate_costs_the_sum_of_the_cores_and_not_the_bounding_box(plate):
    from clinicalomx.extract.builders.mosaic import canvas_shape, estimate_bytes, union_bounds

    out, _detail = plate()
    boxes = [crop_box(_bounds(i)) for i in range(6)]
    canvas = estimate_bytes(canvas_shape(union_bounds(boxes), channels=CH), np.dtype("float32"))
    written = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    cores = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) * CH * 4
    assert cores < canvas, "the fixture grid has no gaps, so the claim is untestable on it"
    assert written < canvas, f"{written} B on disk is no better than a {canvas} B canvas"


def test_peak_does_not_grow_with_the_core_count(plate, monkeypatch):
    watch = _Alive()
    real = ngff.crop
    monkeypatch.setattr(ngff, "crop", lambda *a, **k: watch.saw(real(*a, **k)))
    plate(ids=range(3))
    small = watch.peak
    watch.reset()
    plate(ids=range(6), cores=range(6))
    assert watch.total > 0
    assert watch.peak == small, f"peak grew from {small} to {watch.peak} with the core count"


class _Alive:
    """Bytes held by live crop arrays, sampled as each new one is produced. Weak references."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.refs, self.peak, self.total = [], 0, 0

    def saw(self, result):
        import weakref

        arr, box = result
        self.total += arr.nbytes
        self.refs.append((weakref.ref(arr), arr.nbytes))
        self.peak = max(self.peak, sum(n for r, n in self.refs if r() is not None))
        return arr, box


def test_no_step_ever_materialises_more_than_one_channel(plate, tmp_path):
    from dask.callbacks import Callback

    class _Biggest(Callback):
        def __init__(self):
            self.n = 0

        def _posttask(self, key, result, dsk, state, worker_id):
            self.n = max(self.n, int(getattr(result, "size", 0) or 0))

    x0, y0, x1, y1 = crop_box(_bounds(0))
    plane = (y1 - y0) * (x1 - x0)

    watch = _Biggest()
    with watch:
        plate(ids=range(3))
    assert watch.n > 0, "nothing was materialised at all, so the watch proves nothing"
    assert watch.n <= plane, (
        f"something materialised {watch.n} elements, more than one {plane}-element plane - "
        f"a whole {CH}-channel core is {CH * plane}")

    # The same watch over a whole-core read, to show it can fire.
    fires = _Biggest()
    with fires:
        whole = ngff.open_image(_bgnormed_core(tmp_path / "probe.zarr", 0),
                                variable="adjusted_image")
        ngff.crop(whole, _bounds(0))
    assert fires.n > plane, "the watch cannot see a whole-core read, so it cannot refuse one"
