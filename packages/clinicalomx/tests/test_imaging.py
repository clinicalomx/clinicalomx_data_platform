"""Tests for the image readers: laziness, `probs` never selected by default, channel metadata,
axis order, and crop rebasing. Skipped without the `extract` extra.
"""
from __future__ import annotations

import pytest

pytest.importorskip("xarray", reason="the builders need clinicalomx[extract]")
pytest.importorskip("dask", reason="the builders need clinicalomx[extract]")

import dask.array as da  # noqa: E402
import numpy as np  # noqa: E402
import xarray as xr  # noqa: E402

from clinicalomx.extract.builders.imaging import (  # noqa: E402
    ChannelMeta,
    as_cyx,
    crop,
    describe_store,
    open_image,
    reset_cache,
)

C, Y, X = 6, 400, 500


def _is_lazy(array) -> bool:
    """Dask-backed: nothing read off disk yet."""
    return hasattr(getattr(array, "data", None), "dask")


@pytest.fixture
def bgnormed(tmp_path):
    """A bgnormed zarr with the four variables the real one has."""
    path = tmp_path / "bgnormed.zarr"
    chunks = (1, 128, 128)
    ds = xr.Dataset(
        {
            "adjusted_image": (("c", "y", "x"), da.zeros((C, Y, X), chunks=chunks, dtype="f8")),
            "adjusted_image_post_hoc": (
                ("c", "y", "x"), da.ones((C, Y, X), chunks=chunks, dtype="f8")),
            "labels": (("c", "y", "x"), da.zeros((C, Y, X), chunks=chunks, dtype="u1")),
            "probs": (("c", "c_probs", "y", "x"),
                      da.zeros((C, 3, Y, X), chunks=(1, 3, 128, 128), dtype="f4")),
        },
        coords={
            "c": [f"CD{i}" for i in range(C)],
            "color_rgb": ("c", [f"#{i:02x}00ff" for i in range(C)]),
            "emission_wavelength_nm": ("c", [520.0 + i for i in range(C)]),
            "fluorophore": ("c", [f"Opal{500 + i}" for i in range(C)]),
            "is_unmixed_component": ("c", [False] * 4 + [True] * 2),
            # Per-channel and per-ROI coords, as the real store carries them.
            "background_component": ("c", list(range(C))),
            "signal_component": ("c", [C - i for i in range(C)]),
            "roi_x": 100.0,
            "roi_y": 200.0,
            "roi_width": X,
            "roi_height": Y,
            "signal_units": "counts",
            "autofluorescence_subtracted": True,
            "responsivity_filter_name": "none",
        },
    )
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": 0.4977, "scale_factor_unit": "um"}
    ds.to_zarr(path, mode="w")
    reset_cache()
    return path


# Laziness.
def test_opening_does_not_read_the_pixels(bgnormed):
    assert _is_lazy(open_image(bgnormed, variable="adjusted_image").array)


def test_every_variable_opens_lazily(bgnormed):
    for name in ("adjusted_image", "adjusted_image_post_hoc", "labels"):
        assert _is_lazy(open_image(bgnormed, variable=name).array), name


def test_the_crop_is_the_only_thing_materialised(bgnormed):
    opened = open_image(bgnormed, variable="adjusted_image")
    arr, _box = crop(opened, (100.0, 50.0, 300.0, 250.0))
    assert isinstance(arr, np.ndarray)
    assert arr.size < C * Y * X, "the crop is smaller than the source, so the source was not read"
    assert _is_lazy(opened.array), "opening stayed lazy after a crop was taken from it"


# probs.
def test_a_zarr_with_no_variable_named_refuses_rather_than_guessing(bgnormed):
    with pytest.raises(LookupError, match="no variable named"):
        open_image(bgnormed)


def test_the_refusal_says_what_the_store_actually_holds(bgnormed):
    with pytest.raises(LookupError) as exc:
        open_image(bgnormed)
    assert "adjusted_image" in str(exc.value) and "probs" in str(exc.value)


def test_probs_is_discoverable_without_being_selectable(bgnormed):
    opened = open_image(bgnormed, variable="adjusted_image")
    assert "probs" in opened.variables
    assert opened.meta.source == "zarr:adjusted_image"


def test_an_unknown_variable_names_what_is_actually_there(bgnormed):
    with pytest.raises(LookupError, match="adjusted_image"):
        open_image(bgnormed, variable="no_such_variable")


def test_a_variable_on_a_non_zarr_is_rejected(tmp_path):
    with pytest.raises(LookupError, match="not a zarr"):
        open_image(tmp_path / "slide.qptiff", variable="adjusted_image")


# Channel metadata.
def test_channel_metadata_comes_from_the_coords(bgnormed):
    meta = open_image(bgnormed, variable="adjusted_image").meta
    assert meta.names[0] == "CD0" and meta.n == C
    assert meta.colors[0] == "#0000ff"
    assert meta.emission_nm[0] == 520.0
    assert meta.fluorophore[0] == "Opal500"


def test_px_um_is_microns_and_is_not_called_scale_factor(bgnormed):
    meta = open_image(bgnormed, variable="adjusted_image").meta
    assert meta.px_um == 0.4977
    assert not hasattr(meta, "scale_factor")


def test_unmixed_components_are_distinguished_from_biomarkers(bgnormed):
    meta = open_image(bgnormed, variable="adjusted_image").meta
    assert sum(1 for u in meta.unmixed if not u) == 4


def test_a_missing_optional_coord_is_absent_metadata_not_an_error(tmp_path):
    path = tmp_path / "bare.zarr"
    xr.Dataset(
        {"adjusted_image": (("c", "y", "x"), da.zeros((2, 8, 8), chunks=(1, 8, 8)))},
        coords={"c": ["A", "B"]},
    ).to_zarr(path, mode="w")
    meta = open_image(path, variable="adjusted_image").meta
    assert meta.names == ("A", "B")
    assert meta.colors == (None, None) and meta.px_um is None


# Axis order.
@pytest.mark.parametrize(
    ("shape", "expected"),
    [((8, 9), (1, 8, 9)),            # single plane is one channel
     ((40, 8, 9), (40, 8, 9)),       # already (c, y, x)
     ((8, 9, 3), (3, 8, 9)),         # RGB brightfield arrives channel-LAST
     ((3, 8, 9), (3, 8, 9))],        # 3 leading channels: already (c, y, x)
    ids=["2d", "cyx", "rgb-last", "three-leading"],
)
def test_axis_normalisation(shape, expected):
    assert as_cyx(np.zeros(shape)).shape == expected


# The crop.
def test_the_crop_is_padded_and_clamped_to_the_source(bgnormed):
    opened = open_image(bgnormed, variable="adjusted_image")
    _arr, box = crop(opened, (0.0, 0.0, 100.0, 100.0))
    assert box[0] == 0 and box[1] == 0, "clamped at the origin rather than going negative"
    assert box[2] > 100, "padded beyond the requested box"


def test_a_box_entirely_outside_the_source_is_refused(bgnormed):
    opened = open_image(bgnormed, variable="adjusted_image")
    with pytest.raises(ValueError, match="falls outside"):
        crop(opened, (10_000.0, 10_000.0, 11_000.0, 11_000.0))


# The crop is rebased by the store's own origin.
#: Where the per-core store sits on the slide, away from the corner, and its pixel size.
CORE_ORIGIN_PX = (15_000, 20_000)
CORE_PX_UM = 0.4977
CORE_SIDE = 220


def _core_store(path, *, origin=CORE_ORIGIN_PX, side=CORE_SIDE, value=7.0):
    """A per-core bgnormed zarr: core-sized, with absolute micron y/x coords of its slide crop."""
    ox, oy = origin
    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"),
                            da.full((C, side, side), value, chunks=(1, 64, 64), dtype="f8"))},
        coords={
            "c": [f"CD{i}" for i in range(C)],
            "y": (np.arange(side) + oy) * CORE_PX_UM,
            "x": (np.arange(side) + ox) * CORE_PX_UM,
        },
    )
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": CORE_PX_UM}
    ds.to_zarr(path, mode="w")
    reset_cache()
    return path


def _slide_bounds(*, origin=CORE_ORIGIN_PX, side=CORE_SIDE):
    """The unpadded slide box whose padded form is the extent `_core_store` wrote."""
    from clinicalomx.extract.coords import PAD_RATIO

    ox, oy = origin
    inner = side / PAD_RATIO
    margin = (side - inner) / 2.0
    return (ox + margin, oy + margin, ox + margin + inner, oy + margin + inner)


def test_a_per_core_store_is_cropped_at_its_real_slide_position(tmp_path):
    opened = open_image(_core_store(tmp_path / "core.zarr"), variable="adjusted_image")
    arr, box = crop(opened, _slide_bounds())
    assert arr.shape[0] == C
    assert (arr == 7.0).all(), "the rebased slice read the wrong pixels"
    ox, oy = CORE_ORIGIN_PX
    assert box[0] >= ox and box[1] >= oy, "the box came back in the store's frame, not the slide's"
    assert box[2] <= ox + CORE_SIDE and box[3] <= oy + CORE_SIDE


def test_the_returned_box_is_in_the_parents_frame_so_a_mosaic_can_place_it(tmp_path):
    opened = open_image(_core_store(tmp_path / "core.zarr"), variable="adjusted_image")
    _arr, box = crop(opened, _slide_bounds())
    assert box[:2] != (0, 0)


def test_a_store_with_no_recorded_origin_is_read_in_the_same_frame_as_the_box(bgnormed):
    opened = open_image(bgnormed, variable="adjusted_image")
    assert opened.origin is None
    _arr, box = crop(opened, (0.0, 0.0, 100.0, 100.0))
    assert box[0] == 0 and box[1] == 0


def _no_coords(path, *, origin=CORE_ORIGIN_PX, side=CORE_SIDE, value=7.0):
    """The same per-core store with every coord dropped."""
    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"),
                            da.full((C, side, side), value, chunks=(1, 64, 64), dtype="f8"))},
        coords={"c": [f"CD{i}" for i in range(C)]},
    )
    ds.to_zarr(path, mode="w")
    reset_cache()
    return path


def test_a_store_that_records_nothing_is_placed_at_the_box_that_produced_it(tmp_path):
    from clinicalomx.extract.builders.imaging import DECLARED, resolve_origin

    opened = open_image(_no_coords(tmp_path / "bare.zarr"), variable="adjusted_image")
    assert opened.origin is None, "the fixture recorded something after all"
    assert resolve_origin(opened, _slide_bounds()) == (*CORE_ORIGIN_PX, DECLARED)
    arr, box = crop(opened, _slide_bounds())
    assert (arr == 7.0).all(), "the declared frame read the wrong pixels"
    ox, oy = CORE_ORIGIN_PX
    assert box[0] >= ox and box[2] <= ox + CORE_SIDE, "the box is not in the slide's frame"


def test_a_declared_frame_is_the_frame_the_store_would_have_recorded(tmp_path):
    from clinicalomx.extract.builders.imaging import resolve_origin

    recorded = open_image(_core_store(tmp_path / "rec.zarr"), variable="adjusted_image")
    bare = open_image(_no_coords(tmp_path / "bare.zarr"), variable="adjusted_image")
    assert resolve_origin(bare, _slide_bounds())[:2] == resolve_origin(recorded,
                                                                      _slide_bounds())[:2]
    assert crop(bare, _slide_bounds())[1] == crop(recorded, _slide_bounds())[1]


def test_a_store_that_is_not_this_crop_gets_no_frame_rather_than_a_plausible_one(tmp_path):
    from clinicalomx.extract.builders.imaging import PARENT, resolve_origin

    ux0, uy0, ux1, uy1 = _slide_bounds()
    inner = int(ux1 - ux0)
    opened = open_image(_no_coords(tmp_path / "inner.zarr", side=inner),
                        variable="adjusted_image")
    assert resolve_origin(opened, _slide_bounds()) == (0, 0, PARENT)
    with pytest.raises(ValueError, match="does not match this crop either"):
        crop(opened, _slide_bounds())


#: How far the scan is cut inside the core's own box, in pixels: more than the pad margin.
CLIPPED_BY = 20


def _clipped_scan(*, origin=CORE_ORIGIN_PX, side=CORE_SIDE):
    """``(h, w)`` of a scan whose right and bottom edges fall INSIDE this core's own box."""
    ox, oy = origin
    return (oy + side - CLIPPED_BY, ox + side - CLIPPED_BY)


def test_a_store_cut_short_at_the_edge_of_the_scan_is_placed_at_its_own_box(tmp_path):
    from clinicalomx.extract.builders.imaging import DECLARED, PARENT, resolve_origin

    scan = _clipped_scan()
    opened = open_image(_no_coords(tmp_path / "cut.zarr", side=CORE_SIDE - CLIPPED_BY),
                        variable="adjusted_image")
    assert resolve_origin(opened, _slide_bounds()) == (0, 0, PARENT), (
        "a caller that names no scan must keep refusing: it cannot tell this from another crop")
    assert resolve_origin(opened, _slide_bounds(), parent_shape=scan) == (*CORE_ORIGIN_PX, DECLARED)
    arr, box = crop(opened, _slide_bounds(), parent_shape=scan)
    assert (arr == 7.0).all(), "the declared frame read the wrong pixels"
    assert box[:2] == CORE_ORIGIN_PX, "the core did not land at its own slide position"
    assert box[2] == scan[1] and box[3] == scan[0], "the box ran past the edge of the scan"


def test_a_store_that_is_not_this_crop_is_refused_at_the_edge_too(tmp_path):
    from clinicalomx.extract.builders.imaging import PARENT, resolve_origin

    scan = _clipped_scan()
    ux0, uy0, ux1, uy1 = _slide_bounds()
    inner = min(int(ux1 - ux0), scan[1] - int(ux0))
    opened = open_image(_no_coords(tmp_path / "inner_cut.zarr", side=inner),
                        variable="adjusted_image")
    assert resolve_origin(opened, _slide_bounds(), parent_shape=scan) == (0, 0, PARENT)
    with pytest.raises(ValueError, match="does not match this crop either"):
        crop(opened, _slide_bounds(), parent_shape=scan)


def test_the_refusal_says_what_extent_it_expected_and_whether_a_scan_was_named(tmp_path):
    opened = open_image(_no_coords(tmp_path / "bare2.zarr", side=CORE_SIDE - CLIPPED_BY),
                        variable="adjusted_image")
    with pytest.raises(ValueError, match="named no parent extent"):
        crop(opened, _slide_bounds())
    with pytest.raises(ValueError, match=r"cut to the \d+x\d+ scan"):
        crop(open_image(_no_coords(tmp_path / "inner2.zarr", side=100),
                        variable="adjusted_image"),
             _slide_bounds(), parent_shape=_clipped_scan())


def test_a_box_outside_a_rebased_store_says_where_the_store_actually_is(tmp_path):
    opened = open_image(_core_store(tmp_path / "core.zarr"), variable="adjusted_image")
    with pytest.raises(ValueError, match="records its origin at"):
        crop(opened, (0.0, 0.0, 100.0, 100.0))


def test_the_origin_is_rounded_rather_than_floored(tmp_path):
    from clinicalomx.extract.builders.imaging import RECORDED, resolve_origin

    opened = open_image(_core_store(tmp_path / "core.zarr"), variable="adjusted_image")
    ox, oy, how = resolve_origin(opened, _slide_bounds())
    assert (ox, oy, how) == (*CORE_ORIGIN_PX, RECORDED)


def test_channels_are_counted_from_the_axes_without_a_read(bgnormed):
    from clinicalomx.extract.builders.imaging import channels_of

    assert channels_of(open_image(bgnormed, variable="adjusted_image")) == (C, "")
    n, why = channels_of(open_image(bgnormed, variable="probs"))
    assert n == 0 and "c_probs" in why


def test_channel_meta_is_json_serialisable():
    import json

    json.dumps(ChannelMeta(names=("A",), px_um=0.5).to_dict())


# The multi-series OME-TIFF, written and read back.
def _series_from(paths_and_vars, bounds):
    from clinicalomx.extract.builders.imaging import ChannelMeta as CM

    series, meta = [], CM()
    for label, art, var in paths_and_vars:
        opened = open_image(art, variable=var)
        arr, _box = crop(opened, bounds)
        if str(arr.dtype) == "float64":
            arr = np.nan_to_num(arr).astype(np.float32)
        series.append((label, arr, str(arr.dtype)))
        if opened.meta.n and not meta.n:
            meta = opened.meta
    return series, meta


@pytest.fixture
def raw_tiff(tmp_path):
    import tifffile

    path = tmp_path / "slide.tif"
    tifffile.imwrite(path, np.full((C, 600, 800), 3, dtype=np.uint16))
    return path


def test_three_series_are_written_with_their_own_dtypes(bgnormed, raw_tiff, tmp_path):
    import tifffile

    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    series, meta = _series_from(
        [("raw", raw_tiff, None), ("bgnorm", bgnormed, "adjusted_image"),
         ("bgnormQ", bgnormed, "adjusted_image_post_hoc")], (100.0, 50.0, 300.0, 250.0))
    out = tmp_path / "R0C0.ome.tiff"
    write_ome_tiff(series, to_ome(series, meta), out)

    with tifffile.TiffFile(out) as tf:
        assert len(tf.series) == 3, "a multi-series file, not one long stack"
        from ome_types import from_xml

        ome = from_xml(tf.pages[0].description)
    assert [i.name for i in ome.images] == ["raw", "bgnorm", "bgnormQ"]
    assert [i.pixels.type.value for i in ome.images] == ["uint16", "float", "float"]


def test_the_series_are_pixel_registered_with_each_other(bgnormed, raw_tiff):
    series, _meta = _series_from(
        [("raw", raw_tiff, None), ("bgnorm", bgnormed, "adjusted_image")],
        (100.0, 50.0, 300.0, 250.0))
    assert len({s[1].shape[1:] for s in series}) == 1


def test_channels_are_named_coloured_and_carry_their_emission(bgnormed, tmp_path):
    import tifffile
    from ome_types import from_xml

    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    series, meta = _series_from([("bgnorm", bgnormed, "adjusted_image")], (0.0, 0.0, 200.0, 200.0))
    out = tmp_path / "named.ome.tiff"
    write_ome_tiff(series, to_ome(series, meta), out)

    with tifffile.TiffFile(out) as tf:
        ome = from_xml(tf.pages[0].description)
    first = ome.images[0].pixels.channels[0]
    assert first.name == "CD0"
    assert first.color is not None
    assert first.emission_wavelength == 520.0
    assert first.fluor == "Opal500"


def test_the_physical_pixel_size_survives_in_microns(bgnormed, tmp_path):
    import tifffile
    from ome_types import from_xml

    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    series, meta = _series_from([("bgnorm", bgnormed, "adjusted_image")], (0.0, 0.0, 200.0, 200.0))
    out = tmp_path / "sized.ome.tiff"
    write_ome_tiff(series, to_ome(series, meta), out)
    with tifffile.TiffFile(out) as tf:
        pixels = from_xml(tf.pages[0].description).images[0].pixels
    assert pixels.physical_size_x == 0.4977
    assert pixels.physical_size_x_unit.value == "µm"


def test_a_source_with_no_channel_coords_still_writes(raw_tiff, tmp_path):
    import tifffile

    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    series, meta = _series_from([("raw", raw_tiff, None)], (0.0, 0.0, 200.0, 200.0))
    out = tmp_path / "bare.ome.tiff"
    write_ome_tiff(series, to_ome(series, meta), out)
    with tifffile.TiffFile(out) as tf:
        assert len(tf.series) == 1


# What is inside a store.
def _landmine(shape, dtype):
    """A lazy array that raises the moment anything reads it."""
    def boom(_block):
        raise AssertionError("a data variable was materialised")

    return da.map_blocks(boom, da.zeros(shape, chunks=shape, dtype=dtype), dtype=dtype)


def test_a_store_lists_every_array_it_holds(bgnormed):
    contents = describe_store(bgnormed)
    assert contents.ok, contents.error
    assert [v.name for v in contents.variables] == [
        "adjusted_image", "adjusted_image_post_hoc", "labels", "probs"]


def test_each_array_carries_its_dims_shape_and_dtype(bgnormed):
    by_name = {v.name: v for v in describe_store(bgnormed).variables}
    assert by_name["adjusted_image"].dims == ("c", "y", "x")
    assert by_name["adjusted_image"].shape == (C, Y, X)
    assert by_name["adjusted_image"].dtype == "float64"
    assert by_name["labels"].dtype == "uint8"
    # `probs`: n_components per pixel per channel.
    assert by_name["probs"].dims == ("c", "c_probs", "y", "x")
    assert by_name["probs"].shape == (C, 3, Y, X)


def test_listing_a_store_never_reads_a_data_variable(monkeypatch, tmp_path):
    import clinicalomx.extract.builders.imaging as imaging

    ds = xr.Dataset(
        {"adjusted_image": (("c", "y", "x"), _landmine((C, Y, X), "f8")),
         "probs": (("c", "c_probs", "y", "x"), _landmine((C, 3, Y, X), "f4"))},
        coords={"c": [f"CD{i}" for i in range(C)], "signal_units": "counts"},
    )
    monkeypatch.setattr(imaging, "_open_dataset", lambda _path: ds)

    with pytest.raises(AssertionError, match="materialised"):
        ds["adjusted_image"].values  # noqa: B018 - proves the landmine is armed

    contents = describe_store(tmp_path / "x.zarr")
    assert contents.ok, contents.error
    assert {v.name for v in contents.variables} == {"adjusted_image", "probs"}
    assert {c.name: c.values for c in contents.coords}["signal_units"] == ("counts",)


def test_a_data_variable_never_carries_values(bgnormed):
    assert all(v.values == () for v in describe_store(bgnormed).variables)


def test_the_per_channel_and_per_roi_coords_are_surfaced(bgnormed):
    coords = {c.name: c for c in describe_store(bgnormed).coords}
    assert coords["background_component"].dims == ("c",)
    assert coords["background_component"].values == tuple(str(i) for i in range(C))
    assert coords["signal_units"].values == ("counts",)
    assert coords["roi_width"].values == (str(X),)


def test_a_long_coordinate_is_measured_not_read(tmp_path):
    path = tmp_path / "wide.zarr"
    xr.Dataset(
        {"adjusted_image": (("c", "y"), da.zeros((2, 400), chunks=(1, 400), dtype="f4"))},
        coords={"c": ["CD3", "CD8"], "y": [i * 0.5 for i in range(400)]},
    ).to_zarr(path, mode="w")

    coords = {c.name: c for c in describe_store(path).coords}
    assert coords["y"].shape == (400,)
    assert coords["y"].values == ()
    assert coords["c"].values == ("CD3", "CD8"), "a short coord is still read"


def test_a_key_is_carried_so_the_report_names_the_store_it_opened(bgnormed):
    assert describe_store(bgnormed, key="bgnorm/R0C0.zarr").key == "bgnorm/R0C0.zarr"


def test_a_non_zarr_is_refused_with_a_reason_rather_than_opened(tmp_path):
    contents = describe_store(tmp_path / "slide.qptiff")
    assert not contents.ok
    assert "not a zarr" in contents.error


def test_an_unreadable_store_is_a_reported_state_not_a_crash(tmp_path):
    contents = describe_store(tmp_path / "gone.zarr", key="gone")
    assert not contents.ok and contents.key == "gone"


def test_inspect_stores_marks_which_arrays_an_element_addresses(bgnormed, monkeypatch):
    from clinicalomx import governance
    from clinicalomx.extract import Element, Recipe
    from clinicalomx.extract.availability import (
        describe,
        inspect_stores,
        variables_addressed,
    )
    from clinicalomx.extract.source import Artifact
    from test_snapshot import _loaded, _Rec

    monkeypatch.setattr(governance, "permits_read", lambda _key: True)
    tma = _Rec("uT", "arutha:TMA0", "TMA")
    core = _Rec("uC0", "arutha:TMA0:R0C0", "Core")
    snap = _loaded({"TMA": [tma], "Core": [core]},
                   {"uT": {}, "uC0": {"parent_roi": "arutha:TMA0", "bgnormed": "n/0.zarr"}})
    snap._role_artifacts = [(core, "bgnormed", "n/0.zarr")]
    snap._role_features = ("bgnormed",)
    snap._backrefs = {"n/0.zarr": {"roi_uid": "uC0"}}
    snap.artifact_rows = lambda keys, columns: {k: {"roi_uid": "uC0"} for k in keys}

    recipe = Recipe(
        name="t.series", target="OME-TIFF", roi_types=("TMA",), layout="per_child",
        child_types=("Core",),
        elements=(
            Element("bgnorm", "image", roles=("bgnormed",), variable="adjusted_image"),
            Element("bgnormQ", "image", roles=("bgnormed",),
                    variable="adjusted_image_post_hoc"),
        ),
        builder="m:b", placement="m:p")

    class _Src:
        def artifacts(self, ids):
            return [Artifact(artifact_id=k, uri=str(bgnormed), stored=True) for k in ids]

    av = describe(tma, snapshot=snap, recipes=[recipe], now="2026-01-01T00:00:00Z")
    contents = inspect_stores(av, source=_Src())["bgnormed"]
    assert contents.ok, contents.error

    addressed = variables_addressed(av.child_elements, "bgnormed")
    assert addressed == {"adjusted_image": "bgnorm",
                         "adjusted_image_post_hoc": "bgnormQ"}
    unread = [v.name for v in contents.variables if v.name not in addressed]
    assert unread == ["labels", "probs"], "in every store, and named by nothing"
