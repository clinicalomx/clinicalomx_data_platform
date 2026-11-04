"""Tests for the sparse mosaic: placement, absent gaps, the size ceiling, and the pyramid."""
from __future__ import annotations

import numpy as np
import pytest

from clinicalomx.extract.builders.mosaic import (
    MAX_STORED_BYTES,
    MosaicError,
    Placed,
    Sparse,
    cache_bytes,
    canvas_shape,
    check_footprint,
    estimate_bytes,
    pyramid_levels,
    stored_bytes,
    union_bounds,
)

#: Small enough for a fixture canvas several tiles across. tifffile needs a multiple of 16.
SMALL_TILE = (16, 16)


def _core(name, value, *, x=0, y=0, size=4, channels=2, dtype="uint16"):
    """One core: a dense block at a slide offset, and a reader for one plane of it."""
    arr = np.full((channels, size, size), value, dtype=dtype)
    return Placed(name, (x, y, x + size, y + size), lambda c, a=arr: a[c])


def _mosaic(sources, bounds, **kw):
    """A :class:`Sparse` over ``sources``, declared from the first one."""
    sources = list(sources)
    kw.setdefault("channels", 2)
    kw.setdefault("dtype", "uint16")
    channels, dtype = kw.pop("channels"), kw.pop("dtype")
    downsample = kw.pop("downsample", 1)
    return Sparse(canvas_shape(bounds, channels=channels, downsample=downsample), dtype,
                  int(bounds[0]), int(bounds[1]), sources=tuple(sources), downsample=downsample,
                  tile=kw.pop("tile", SMALL_TILE), **kw)


def _dense(sparse):
    """Reassemble the tile stream into a dense array. Tests only."""
    channels, height, width = sparse.shape
    th, tw = sparse.tile
    out = np.zeros(sparse.shape, np.dtype(sparse.dtype))
    tiles = sparse.tiles()
    for c in range(channels):
        for y in range(0, height, th):
            for x in range(0, width, tw):
                tile = next(tiles)
                if tile is not None:
                    out[c, y:y + tile.shape[0], x:x + tile.shape[1]] = tile
    with pytest.raises(StopIteration):
        next(tiles)
    return out


# The canvas is the cores, not the slide.
def test_the_canvas_is_the_union_of_the_cores():
    assert union_bounds([(100, 200, 110, 210), (300, 50, 320, 80)]) == (100, 50, 320, 210)


def test_the_canvas_records_where_it_sits_on_the_slide():
    one = _mosaic([_core("a", 1, x=1000, y=2000)], (1000, 2000, 1004, 2004))
    assert (one.origin_x, one.origin_y) == (1000, 2000)


def test_a_core_lands_at_its_own_offset_not_at_the_origin():
    arr = _dense(_mosaic([_core("a", 7, x=100, y=100), _core("b", 9, x=110, y=104)],
                         (100, 100, 114, 108)))
    assert arr.shape == (2, 8, 14)
    assert (arr[:, 0:4, 0:4] == 7).all(), "core a is not where it belongs"
    assert (arr[:, 4:8, 10:14] == 9).all(), "core b is not where it belongs"
    assert arr[0, 6, 0] == 0, "the gap between cores should stay empty"


def test_every_core_reaches_the_mosaic():
    one = _mosaic([_core(f"c{i}", i + 1, x=i * 10, y=0) for i in range(6)], (0, 0, 54, 4))
    _dense(one)
    assert one.placed == [f"c{i}" for i in range(6)]
    assert not one.skipped


# The gaps.
def test_a_tile_no_core_covers_is_yielded_as_absent():
    one = _mosaic([_core("a", 5, x=0, y=0, size=16), _core("b", 6, x=48, y=0, size=16)],
                  (0, 0, 64, 16))
    tiles = list(one.tiles())
    assert len(tiles) == 2 * 1 * 4, "one tile per channel per grid cell"
    assert [t is None for t in tiles[:4]] == [False, True, True, False]
    assert one.empty_tiles == 4 and one.stored_tiles == 4


def test_the_absent_tiles_read_back_as_zero(tmp_path):
    from clinicalomx.extract.builders.imaging import ChannelMeta
    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    one = _mosaic([_core("a", 5, x=0, y=0, size=16), _core("b", 6, x=48, y=0, size=16)],
                  (0, 0, 64, 16))
    out = tmp_path / "sparse.ome.tiff"
    series = [("m", one, str(one.dtype))]
    write_ome_tiff(series, to_ome(series, ChannelMeta()), out)

    import tifffile as tf

    with tf.TiffFile(out) as handle:
        counts = handle.pages[0].databytecounts
        arr = handle.asarray()
    assert list(counts).count(0) == 2, "the gap tiles were stored after all"
    assert (arr[:, :, 0:16] == 5).all() and (arr[:, :, 48:64] == 6).all()
    assert (arr[:, :, 16:48] == 0).all(), "an absent tile did not read back as zero"


def test_a_sparse_mosaic_is_smaller_than_the_canvas_it_describes(tmp_path):
    from clinicalomx.extract.builders.imaging import ChannelMeta
    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    cores = [_core(f"c{i}", i + 1, x=i * 1024, y=0, size=64, channels=4) for i in range(8)]
    one = _mosaic(cores, (0, 0, 8 * 1024, 1024), channels=4, tile=(512, 512))
    out = tmp_path / "wide.ome.tiff"
    series = [("m", one, str(one.dtype))]
    write_ome_tiff(series, to_ome(series, ChannelMeta()), out)
    dense = estimate_bytes(one.shape, one.dtype)
    assert out.stat().st_size < dense / 50, (
        f"{out.stat().st_size} B against a {dense} B canvas; the gaps were stored")


# What it refuses to guess.
def test_overlap_is_reported_and_the_first_write_kept():
    one = _mosaic([_core("a", 5, x=0, y=0), _core("b", 8, x=2, y=0)], (0, 0, 6, 4))
    arr = _dense(one)
    assert one.overlaps, "a collision was not reported"
    assert (arr[:, :, 0:4] == 5).all(), "the first write did not win"
    assert (arr[:, :, 4:6] == 8).all(), "the non-overlapping part of b was dropped"


def test_a_core_unreadable_before_it_contributes_anything_is_named_and_the_rest_survive():
    def boom(_c):
        raise OSError("the store went away")

    one = _mosaic([_core("good", 5, x=0, y=0), Placed("bad", (10, 0, 14, 4), boom)],
                  (0, 0, 14, 4))
    arr = _dense(one)
    assert (arr[:, :, 0:4] == 5).all()
    assert (arr[:, :, 10:14] == 0).all()
    assert any("bad at channel 0" in s and "OSError" in s for s in one.skipped)


def test_a_decimated_plane_does_not_keep_its_full_resolution_source_alive():
    import weakref

    sources = []

    def read(channel):
        plane = np.full((64, 64), 7, dtype="uint16")
        sources.append(weakref.ref(plane))
        return plane

    one = _mosaic([Placed("core", (0, 0, 64, 64), read)], (0, 0, 64, 64), downsample=4)
    tiles = one.tiles()
    tile = next(tiles)
    assert (tile == 7).all()
    assert sources and all(ref() is None for ref in sources), (
        "the full-resolution plane is still reachable behind its decimation")


def test_a_core_that_dies_part_way_through_its_channels_abandons_the_series():
    def dies_after_the_first(channel):
        if channel:
            raise OSError("the store went away")
        return np.full((4, 4), 7, "uint16")

    one = _mosaic([_core("good", 5, x=0, y=0), Placed("bad", (10, 0, 14, 4), dies_after_the_first)],
                  (0, 0, 14, 4))
    with pytest.raises(MosaicError, match="channel 1"):
        list(one.tiles())
    assert any("bad at channel 1" in s and "OSError" in s for s in one.skipped)


def test_the_stored_byte_figure_counts_a_partial_edge_tile_as_partial():
    one = _mosaic([_core("a", 5, x=0, y=0, size=20)], (0, 0, 20, 20))
    written = [t for t in one.tiles() if t is not None]
    th, tw = one.tile
    assert one.stored_bytes == sum(int(t.nbytes) for t in written)
    assert one.stored_bytes < one.stored_tiles * th * tw * np.dtype("uint16").itemsize


# Size: the ceiling counts the cores, not the canvas.
#: A real TMA's canvas, (width, height) in pixels.
HIS_CANVAS = (49_429, 29_111)
HIS_CHANNELS = 64


def _grid(cores, side, *, across, pitch):
    """``cores`` padded boxes on a regular grid."""
    return [(pitch * (i % across), pitch * (i // across),
             pitch * (i % across) + side, pitch * (i // across) + side) for i in range(cores)]


def test_a_tile_two_cores_share_is_stored_once():
    alone = stored_bytes([(0, 0, 600, 600)], "uint16", channels=1)
    together = stored_bytes([(0, 0, 600, 600), (100, 0, 700, 600)], "uint16", channels=1)
    assert together < alone * 2, "the shared tiles were billed to both cores"


def test_the_ceiling_counts_the_cores_and_not_the_canvas():
    boxes = _grid(18, 2048, across=5, pitch=6000)
    canvas = estimate_bytes(canvas_shape(union_bounds(boxes), channels=64), np.dtype("uint16"))
    assert stored_bytes(boxes, "uint16", channels=64) < canvas / 4


def test_the_ceiling_counts_whole_tiles_and_not_the_core_boxes():
    boxes = _grid(4, 2048, across=2, pitch=6000)
    area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
    tiled = stored_bytes(boxes, "uint16", channels=1) // np.dtype("uint16").itemsize
    assert tiled > area, "the tile margin around every core is not being counted"
    assert tiled < area * 2, "the whole-tile figure has run away from the cores it describes"


def test_the_full_resolution_tma_that_was_refused_now_builds():
    boxes = _grid(18, 5632, across=5, pitch=HIS_CANVAS[0] // 5)
    canvas = estimate_bytes(canvas_shape(union_bounds(boxes), channels=HIS_CHANNELS),
                            np.dtype("uint16"))
    assert canvas > 8 * 1024**3, "the fixture is not the case that was refused"
    check_footprint(boxes, "uint16", channels=HIS_CHANNELS)


def _levels(boxes, dtype, channels, downsample=1):
    return pyramid_levels(union_bounds(boxes), downsample=downsample)


def test_a_slide_scale_merged_object_is_still_refused():
    boxes = _grid(72, 5632, across=10, pitch=7000)
    with pytest.raises(MosaicError) as err:
        check_footprint(boxes, "float32", channels=64,
                        downsamples=_levels(boxes, "float32", 64))
    message = str(err.value)
    assert "STORE" in message, "the refusal must say which quantity it is refusing"
    assert "stored pixels of every level and not the canvas" in message
    assert "GB" in message, "a refusal that does not say how big is not actionable"
    assert "downsample" in message and "omezarr.tma.plate" in message


def test_the_refusal_says_the_number_covers_the_whole_pyramid():
    boxes = _grid(72, 5632, across=10, pitch=7000)
    with pytest.raises(MosaicError) as err:
        check_footprint(boxes, "float32", channels=64,
                        downsamples=_levels(boxes, "float32", 64))
    message = str(err.value)
    assert "pyramid levels" in message, "the refusal must say how many levels it counted"
    assert "of which the base is" in message, "a reader has to be able to separate the two"


def test_the_suggested_downsample_actually_fits():
    boxes = _grid(72, 5632, across=10, pitch=7000)
    with pytest.raises(MosaicError) as err:
        check_footprint(boxes, "float32", channels=64,
                        downsamples=_levels(boxes, "float32", 64))
    factor = int(str(err.value).split("downsample>=")[1].split()[0])
    check_footprint(boxes, "float32", channels=64,
                    downsamples=_levels(boxes, "float32", 64, factor))


def test_an_empty_canvas_no_longer_makes_a_build_ruinous():
    tight = _grid(18, 2048, across=5, pitch=2400)
    loose = _grid(18, 2048, across=5, pitch=240_000)
    close, far = (stored_bytes(b, "uint16", channels=64) for b in (tight, loose))
    assert abs(far - close) < close * 0.05
    check_footprint(loose, "uint16", channels=64)


def test_downsampling_shrinks_the_canvas_and_keeps_the_offsets():
    one = _mosaic([_core("a", 3, x=0, y=0, size=8), _core("b", 4, x=16, y=0, size=8)],
                  (0, 0, 24, 8), downsample=2)
    arr = _dense(one)
    assert arr.shape == (2, 4, 12)
    assert (arr[:, :, 0:4] == 3).all()
    assert (arr[:, :, 8:12] == 4).all()
    assert one.downsample == 2


def test_estimate_counts_every_channel():
    assert estimate_bytes((45, 1000, 1000), np.dtype("uint16")) == 45 * 1000 * 1000 * 2


def test_the_ceiling_is_the_shipped_one():
    assert MAX_STORED_BYTES == 256 * 1024**3


# The builder, end to end, over per-core stores at slide offsets.
pytest.importorskip("tifffile", reason="the OME-TIFF writer needs clinicalomx[extract]")
pytest.importorskip("xarray", reason="the OME-TIFF writer needs clinicalomx[extract]")

import dask.array as da  # noqa: E402
import tifffile  # noqa: E402
import xarray as xr  # noqa: E402

from clinicalomx.extract.builders import ometiff  # noqa: E402
from clinicalomx.extract.builders.imaging import reset_cache  # noqa: E402
from clinicalomx.extract.coords import crop_box  # noqa: E402

CH = 3
#: The unpadded detection box of one core, and its grid, which starts away from the slide corner.
INNER = 88
PITCH = 140
GRID_W = 3
SLIDE_X, SLIDE_Y = 300, 500
PX_UM = 0.5


def _bounds(i, pitch=PITCH):
    """Core ``i``'s unpadded slide box."""
    x = SLIDE_X + (i % GRID_W) * pitch
    y = SLIDE_Y + (i // GRID_W) * pitch
    return (float(x), float(y), float(x + INNER), float(y + INNER))


def _bgnormed_core(path, i, *, pitch=PITCH, dtype="f8", no_origin=False, unpadded=False,
                   scan=None):
    """One core's bgnormed zarr, as the pipeline writes it: core-sized, at the padded box.

    Records its micron y/x coords but no ``image_info``. ``scan`` is ``(h, w)`` of the slide the
    crop is clamped to.
    """
    x0, y0, x1, y1 = crop_box(_bounds(i, pitch))
    if unpadded:
        # Written at the unpadded box: the wrong field by one pad margin.
        x0, y0, x1, y1 = (int(v) for v in _bounds(i, pitch))
        no_origin = True
    if scan is not None:
        sh, sw = scan
        x0, x1 = max(0, x0), min(int(sw), x1)
        y0, y1 = max(0, y0), min(int(sh), y1)
    h, w = y1 - y0, x1 - x0
    ds = xr.Dataset(
        {
            "adjusted_image": (("c", "y", "x"),
                               da.full((CH, h, w), float(i + 1), chunks=(1, 32, 32), dtype=dtype)),
            "adjusted_image_post_hoc": (
                ("c", "y", "x"),
                da.full((CH, h, w), float(100 + i), chunks=(1, 32, 32), dtype=dtype)),
        },
        coords=(
            {"c": [f"CD{j}" for j in range(CH)]} if no_origin else
            {
                "c": [f"CD{j}" for j in range(CH)],
                "y": (np.arange(h) + y0) * PX_UM,
                "x": (np.arange(w) + x0) * PX_UM,
            }
        ),
    )
    ds.to_zarr(path, mode="w")
    return path


class _Element:
    def __init__(self, name, variable=None):
        self.name, self.kind, self.variable = name, "image", variable


class _Placement:
    """A placement that reads the box off the ROI."""

    coordinate_space = "global"

    def bounds(self, *, values, geometry_row=None, container_attrs=None):
        return values["box"]


class _Type:
    name = "Core"


class _Child:
    """A stand-in Core Record with uid, name and type."""

    type = _Type()

    def __init__(self, i):
        self.uid, self.name = f"uC{i}", f"R0C{i}"


class _Recipe:
    elements = (_Element("raw"), _Element("bgnorm", "adjusted_image"),
                _Element("bgnormQ", "adjusted_image_post_hoc"))

    def __init__(self, n):
        self._children = [_Child(i) for i in range(n)]

    def targets_under(self, _roi, _snapshot):
        return list(self._children)


class _Unit:
    roi_name, roi_uid = "slide:TMA0", "uid0"

    def __init__(self, output, n):
        self.output = str(output)
        self.elements = {"raw": "source_image", "bgnorm": "bgnormed", "bgnormQ": "bgnormed"}
        self.child_elements = {f"R0C{i}": dict(self.elements) for i in range(n)}


class _Snapshot:
    def record_by_uid(self, _uid):
        return "TMA0"


def _sparse_slide(path, channels, height, width):
    """The parent scan, written with every tile absent."""
    tiles = ((height + 511) // 512) * ((width + 511) // 512) * channels

    def stream():
        # tifffile reads the first item's dtype and shape, so it may not be None.
        yield np.zeros((512, 512), np.uint16)
        for _ in range(tiles - 1):
            yield None

    with tifffile.TiffWriter(path, bigtiff=True, ome=False) as handle:
        handle.write(stream(), shape=(channels, height, width),
                     dtype=np.uint16, photometric="minisblack", metadata=False,
                     contiguous=False, compression="zlib", tile=(512, 512))


@pytest.fixture
def tma(tmp_path, monkeypatch):
    """A whole TMA on disk. Only `_resolve` and `_artifact_for` are stubbed."""
    def _make(n=6, *, downsample=1, cores=None, pitch=PITCH, odd_dtype=None, no_origin=(),
              unpadded=(), scan=None, tag=""):
        cores = range(n) if cores is None else cores
        # `scan` shrinks the slide so the outer cores' padded boxes run off it.
        tall, wide = scan or (SLIDE_Y + (n // GRID_W + 2) * pitch, SLIDE_X + (GRID_W + 1) * pitch)
        zarrs = {f"R0C{i}": _bgnormed_core(
            tmp_path / f"c{tag}{i}.zarr", i, pitch=pitch, no_origin=i in no_origin,
            unpadded=i in unpadded, scan=(tall, wide),
            dtype=odd_dtype if (odd_dtype and i) else "f8") for i in cores}
        raw = tmp_path / "slide.tiff"
        _sparse_slide(raw, CH, tall, wide)

        monkeypatch.setattr(ometiff, "_resolve", lambda stub, _s: (
            stub.roi_name,
            {"roi_id": stub.roi_name, "box": _bounds(int(stub.roi_name[3:]), pitch)}, None, None))
        monkeypatch.setattr(ometiff, "_artifact_for", lambda roi, role, *_a, **_k: (
            zarrs.get(roi) if role == "bgnormed" else raw))
        reset_cache()
        out = tmp_path / f"tma{tag}{n}.ome.tiff"
        unit = _Unit(out, n)
        detail = ometiff.build_tma_mosaic(unit, recipe=_Recipe(n), placement=_Placement(),
                                          snapshot=_Snapshot(), downsample=downsample)
        return out, detail
    return _make


class _Alive:
    """Bytes held by live planes, sampled as each new one is produced. Holds weak references."""

    def __init__(self):
        self.refs, self.peak, self.total = [], 0, 0

    def wrap(self, read):
        import weakref

        def watched(channel):
            plane = read(channel)
            self.total += plane.nbytes
            self.refs.append((weakref.ref(plane), plane.nbytes))
            self.peak = max(self.peak, sum(n for r, n in self.refs if r() is not None))
            return plane

        return watched


#: The real `_place`, captured at import.
_REAL_PLACE = ometiff._place

#: A grid pitch that makes the canvas over 100 GB while every store stays one small core.
RUINOUS_PITCH = 60_000


def _watched(monkeypatch):
    from dataclasses import replace

    watch = _Alive()

    def place(*a, **k):
        got = _REAL_PLACE(*a, **k)
        return replace(got, sources=tuple(
            replace(p, read=watch.wrap(p.read)) for p in got.sources))

    monkeypatch.setattr(ometiff, "_place", place)
    return watch


def _cache_watch(monkeypatch):
    """Peak bytes held in the overview caches, sampled after every fill.

    Patches `_Cache.fill` on the class, so `Sparse.release` still finds each cache.
    """
    from clinicalomx.extract.builders.mosaic import _Cache

    # Keyed on identity: `_Cache` is unhashable.
    seen, peak = {}, [0]
    real = _Cache.fill

    def fill(self, channel):
        plane = real(self, channel)
        seen[id(self)] = self
        peak[0] = max(peak[0], sum(p.nbytes for c in seen.values() for p in c.planes.values()))
        return plane

    monkeypatch.setattr(_Cache, "fill", fill)
    return peak


# The recipe makes the object it claims to.
def test_the_mosaic_actually_contains_bgnorm_and_bgnormq(tma):
    path, detail = tma()
    with tifffile.TiffFile(path) as tif:
        assert [s.name for s in tif.series] == ["raw", "bgnorm", "bgnormQ"]
        assert [str(s.dtype) for s in tif.series] == ["uint16", "float32", "float32"]
    assert "3 series" in detail


def test_every_core_lands_in_the_mosaic(tma):
    _path, detail = tma(n=6)
    assert ("placed cores per series: raw 6 of 6 at 3c; bgnorm 6 of 6 at 3c; "
            "bgnormQ 6 of 6 at 3c") in detail
    assert "SKIPPED" not in detail


def test_each_core_keeps_its_own_pixels_at_its_own_offset(tma):
    path, _detail = tma(n=6)
    arr = tifffile.imread(path, series=1)
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    for i in range(6):
        bx, by, _bx1, _by1 = crop_box(_bounds(i))
        assert arr[0, by - y0 + 4, bx - x0 + 4] == float(i + 1), f"core {i} is not where it belongs"


def test_the_gaps_between_the_cores_are_absent_from_the_file(tma):
    path, detail = tma(n=6, pitch=8000)
    with tifffile.TiffFile(path) as handle:
        counts = list(handle.pages[0].databytecounts)
        arr = handle.asarray()
    assert counts.count(0) > len(counts) * 0.9, "most of an 8000 px pitch canvas should be absent"
    assert (arr[0, -512:, -512:] == 0).all(), "an absent tile did not read back as zero"
    assert "left ABSENT" in detail


def test_a_canvas_that_is_mostly_gap_costs_the_file_almost_nothing(tma):
    tight, _d = tma(n=6, pitch=2000)
    reset_cache()
    loose, _d = tma(n=6, pitch=8000)
    assert loose.stat().st_size < tight.stat().st_size * 1.5, (
        f"{loose.stat().st_size} B against {tight.stat().st_size} B for 16x the canvas")


# Every series holds the same cores, or the build does not happen.
def test_every_core_in_raw_is_in_every_series_at_its_own_coordinates(tma):
    path, _detail = tma(n=6)
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    for si, value in ((1, lambda i: float(i + 1)), (2, lambda i: float(100 + i))):
        arr = tifffile.imread(path, series=si)
        for i in range(6):
            ux0, uy0, ux1, uy1 = _bounds(i)
            got = arr[0, int((uy0 + uy1) // 2) - y0, int((ux0 + ux1) // 2) - x0]
            assert got == value(i), f"series {si} is missing core {i} (read {got})"


def test_a_core_whose_store_cannot_be_placed_refuses_the_whole_build(tma, tmp_path):
    with pytest.raises(ValueError, match="could not be placed"):
        tma(n=6, unpadded=(2, 4))
    assert not list(tmp_path.glob("*.ome.tiff")), "nothing may be written for a hollow series"


def test_the_refusal_names_every_core_and_never_truncates(tma):
    with pytest.raises(ValueError) as caught:
        tma(n=6, unpadded=(1, 2, 3, 4, 5))
    said = str(caught.value)
    assert "none truncated" in said
    for i in range(1, 6):
        assert f"R0C{i}" in said, f"core {i} is not named in the refusal"
    assert "(raw 6, bgnorm 1, bgnormQ 1)" in said, "the per-series counts are the evidence"


def test_the_detail_counts_the_placed_cores_of_every_series(tma):
    _path, detail = tma(n=6, cores=[0, 1, 2])
    assert ("placed cores per series: raw 6 of 6 at 3c; bgnorm 3 of 3 at 3c; "
            "bgnormQ 3 of 3 at 3c") in detail


def test_a_series_legitimately_holding_fewer_cores_says_which_ones(tma):
    _path, detail = tma(n=6, cores=[0, 1, 2])
    assert "NOT EVERY SERIES HOLDS EVERY CORE" in detail
    for core in ("R0C3", "R0C4", "R0C5"):
        assert core in detail


def test_the_file_itself_states_each_series_own_core_count(tma):
    from ome_types import from_xml

    path, _detail = tma(n=6, cores=[0, 1, 2])
    with tifffile.TiffFile(path) as handle:
        ome = from_xml(handle.pages[0].description)
    said = {image.name: image.description for image in ome.images}
    assert "6 PLACED cores" in said["raw"]
    assert "3 PLACED cores" in said["bgnorm"]
    assert "3 PLACED cores" in said["bgnormQ"]


def test_a_series_no_core_could_supply_is_named_rather_than_dropped(tma):
    _path, detail = tma(n=6, cores=[])
    assert "raw" in detail
    assert "MISSING series" in detail and "bgnorm" in detail and "bgnormQ" in detail


# Peak memory: the cores crossing one tile row, at one channel.
def test_peak_is_one_grid_row_of_planes_rather_than_the_whole_tma(tma, monkeypatch):
    watch = _watched(monkeypatch)
    tma(n=12, pitch=1200)
    biggest = max(n for _r, n in watch.refs)
    assert watch.peak <= biggest * GRID_W, (
        f"peak {watch.peak} B is more than one grid row of planes ({biggest * GRID_W} B)")
    assert watch.total > watch.peak * 10, "the fixture is too small for the claim to mean anything"


def test_peak_does_not_grow_with_the_core_count(tma, monkeypatch):
    small = _watched(monkeypatch)
    tma(n=3, pitch=1200)
    reset_cache()
    big = _watched(monkeypatch)
    tma(n=12, pitch=1200)
    assert big.total > small.total * 3, "the fixture did not actually get bigger"
    assert big.peak == small.peak, f"peak grew from {small.peak} to {big.peak} with the core count"


def test_no_step_ever_materialises_a_whole_core(tma, monkeypatch):
    watch = _watched(monkeypatch)
    tma(n=6)
    single = max(n for _r, n in watch.refs)
    assert len(watch.refs) == 6 * CH * 3, "a plane was not read once per core, channel and series"
    assert single * CH * 6 < watch.total * 1.01


# The refusal, and when it arrives.
def test_the_ceiling_refuses_before_a_single_pixel_is_read(tma, monkeypatch):
    watch = _watched(monkeypatch)
    monkeypatch.setattr("clinicalomx.extract.builders.mosaic.MAX_STORED_BYTES", 1024)
    with pytest.raises(ValueError, match="over the"):
        tma(n=6)
    assert watch.total == 0, "a pixel was read before the size was refused"


def test_the_refusal_still_says_how_big_and_what_to_do(tma, monkeypatch):
    monkeypatch.setattr("clinicalomx.extract.builders.mosaic.MAX_STORED_BYTES", 1024)
    with pytest.raises(ValueError) as err:
        tma(n=6)
    assert "STORE" in str(err.value) and "downsample>=" in str(err.value)


def test_a_mostly_empty_canvas_is_no_longer_refused(tma):
    path, detail = tma(n=6, pitch=RUINOUS_PITCH)
    assert "raw 6 of 6 at 3c; bgnorm 6 of 6 at 3c; bgnormQ 6 of 6 at 3c" in detail
    assert path.stat().st_size < 50 * 1024**2


# What the written file says.
def test_the_ceiling_figure_is_what_the_build_actually_stores(tma, monkeypatch):
    from clinicalomx.extract.builders import mosaic as mosaic_module

    made, real = [], mosaic_module.Sparse

    def record(*a, **k):
        one = real(*a, **k)
        made.append(one)
        return one

    monkeypatch.setattr(mosaic_module, "Sparse", record)
    tma(n=6, pitch=8000)
    bounds = union_bounds([crop_box(_bounds(i, 8000)) for i in range(6)])
    assert made, "no series was written"
    for one in made:
        predicted = stored_bytes([p.box for p in one.sources], one.dtype,
                                 channels=one.shape[0], downsample=one.downsample, bounds=bounds)
        assert predicted == one.stored_bytes
        if one.decimate is None:
            # Base levels only: a deep overview may have no gaps.
            assert one.empty_tiles > one.stored_tiles, "the fixture has no gaps left to measure"


def test_a_series_no_core_could_supply_is_named_on_its_own_terms(tma, monkeypatch):
    from clinicalomx.extract.builders.mosaic import EmptyMosaic

    def place(targets, element, skipped):
        if element.name != "bgnorm":
            return _REAL_PLACE(targets, element, skipped)
        for target in targets:
            skipped.append(f"{target.where}/bgnorm (OSError: the store went away)")
        raise EmptyMosaic("every core failed to open")

    monkeypatch.setattr(ometiff, "_place", place)
    _path, detail = tma(n=6)
    assert "MISSING series bgnorm" in detail
    assert "SKIPPED" in detail, "the per-core reasons are diagnosis and must survive too"


def test_the_written_file_records_where_the_canvas_sits_on_the_slide(tma):
    from ome_types import from_xml

    path, _detail = tma()
    with tifffile.TiffFile(path) as tif:
        ome = from_xml(tif.pages[0].description)
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    assert f"origin_x={x0}" in ome.images[0].description
    assert f"origin_y={y0}" in ome.images[0].description


def test_the_channels_survive_into_the_mosaic(tma):
    from ome_types import from_xml

    path, _detail = tma()
    with tifffile.TiffFile(path) as tif:
        ome = from_xml(tif.pages[0].description)
    assert [c.name for c in ome.images[0].pixels.channels] == [f"CD{i}" for i in range(CH)]


def test_the_build_writes_no_scratch_at_all(tma):
    path, _detail = tma()
    assert not list(path.parent.glob("*scratch*"))


def test_a_core_disagreeing_with_the_declaration_stops_the_build(tma):
    with pytest.raises(ValueError, match="dtype"):
        tma(n=6, odd_dtype="u2")


def test_a_core_dying_part_way_through_its_channels_leaves_no_file(tma, tmp_path, monkeypatch):
    from dataclasses import replace

    def dies_after_the_first(read):
        def watched(channel):
            if channel:
                raise OSError("the store went away")
            return read(channel)
        return watched

    def place(*a, **k):
        got = _REAL_PLACE(*a, **k)
        return replace(got, sources=tuple(
            replace(p, read=dies_after_the_first(p.read)) if p.name == "R0C2" else p
            for p in got.sources))

    monkeypatch.setattr(ometiff, "_place", place)
    with pytest.raises(ValueError, match="R0C2 at channel 1"):
        tma(n=6)
    assert not list(tmp_path.glob("*.ome.tiff")), "a band-inconsistent file was left behind"


def test_the_detail_says_how_many_cores_and_where(tma):
    _path, detail = tma()
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    assert ("placed cores per series: raw 6 of 6 at 3c; bgnorm 6 of 6 at 3c; "
            "bgnormQ 6 of 6 at 3c") in detail
    assert f"origin ({x0}, {y0})" in detail
    assert "tiles written" in detail


# The platform's own readers, against a sparse mosaic.
def test_the_layers_own_reader_opens_a_sparse_mosaic(tma):
    from clinicalomx.extract.builders.imaging import open_image

    path, _detail = tma(n=6, pitch=8000)
    opened = open_image(path)
    arr = np.asarray(opened.array[:, -256:, -256:])
    assert arr.shape == (CH, 256, 256)
    assert (arr == 0).all(), "an absent tile did not read back as zero through the layer's reader"


# The pyramid.
#: A pitch whose canvas exceeds `PYRAMID_FLOOR`, with a gap at the first overview.
PYRAMID_PITCH = 3000


def _tile_bytes(path, page):
    """The raw stored bytes of one page's tiles."""
    with open(path, "rb") as handle:
        out = []
        for offset, count in zip(page.dataoffsets, page.databytecounts, strict=True):
            if not count:
                out.append(b"")
                continue
            handle.seek(offset)
            out.append(handle.read(count))
    return out


def test_pyramid_levels_stop_once_a_viewer_can_render_the_whole_thing():
    assert pyramid_levels((0, 0, 900, 900)) == (1,)
    factors = pyramid_levels((0, 0, 8000, 8000))
    assert factors[0] == 1, "the base level must stay full resolution"
    assert 8000 / factors[-1] <= 1024
    assert 8000 / factors[-2] > 1024, "one level more than the floor needs is a level nobody reads"


def test_the_pyramid_is_every_power_of_two_as_the_source_qptiff_is():
    his = (0, 0, HIS_CANVAS[1], HIS_CANVAS[0])
    assert pyramid_levels(his) == (1, 2, 4, 8, 16, 32, 64)
    assert pyramid_levels(his, downsample=2) == (2, 4, 8, 16, 32, 64)


def test_the_captains_tma_caches_at_four_inside_the_budget():
    from clinicalomx.extract.builders.mosaic import PYRAMID_TOP_BYTES, cache_factor

    boxes = _grid(18, 5632, across=5, pitch=HIS_CANVAS[0] // 5)
    levels = pyramid_levels(union_bounds(boxes))
    for dtype in ("uint16", "float32"):
        cache = cache_factor(levels, boxes, dtype, channels=HIS_CHANNELS)
        assert cache == 4, f"{dtype} caches at {cache}"
        held = cache_bytes(boxes, dtype, channels=HIS_CHANNELS, downsample=cache)
        assert held <= PYRAMID_TOP_BYTES, f"{dtype}: the cached cores are {held} B, over budget"
        finer = cache_bytes(boxes, dtype, channels=HIS_CHANNELS, downsample=cache // 2)
        assert finer > PYRAMID_TOP_BYTES, f"{dtype}: the cache is coarser than the budget needs"


def test_a_modest_canvas_caches_at_the_first_overview():
    from clinicalomx.extract.builders.mosaic import cache_factor

    boxes = _grid(18, 2048, across=5, pitch=4000)
    levels = pyramid_levels(union_bounds(boxes))
    assert levels[:3] == (1, 2, 4)
    assert cache_factor(levels, boxes, "uint16", channels=20) == 2


def test_the_merged_file_carries_overview_levels(tma):
    path, detail = tma(n=6, pitch=PYRAMID_PITCH)
    with tifffile.TiffFile(path) as handle:
        assert [s.name for s in handle.series] == ["raw", "bgnorm", "bgnormQ"]
        for one in handle.series:
            assert len(one.levels) > 1, f"{one.name} is not pyramidal"
            edges = [max(lv.shape[-2:]) for lv in one.levels]
            assert edges == sorted(edges, reverse=True), "the levels are not in descending order"
            assert edges[-1] <= 1024
    assert "PYRAMID" in detail and "overview level" in detail


def test_the_merged_file_has_the_source_qptiffs_structure(tma):
    path, _detail = tma(n=6, pitch=PYRAMID_PITCH)
    with tifffile.TiffFile(path) as handle:
        for one in handle.series:
            base = one.levels[0].shape[-1]
            factors = [round(base / level.shape[-1]) for level in one.levels]
            assert factors == [2 ** i for i in range(len(factors))], (
                f"{one.name} skips a power of two: {factors}")
            assert len(factors) >= 4, f"{one.name}: the fixture is too small to show a chain"
            assert len(handle.pages[0].subifds or ()) == len(factors) - 1, "levels not in SubIFDs"
            for index, level in enumerate(one.levels):
                pages = [pg.aspage() for pg in level.pages if pg is not None]
                assert len(pages) == CH, f"{one.name} level {index}: not one page per channel"
                for page in pages:
                    assert (page.tilelength, page.tilewidth) == (512, 512)
                    assert page.compression == tifffile.COMPRESSION.ADOBE_DEFLATE
                    assert page.photometric == tifffile.PHOTOMETRIC.MINISBLACK
                    assert page.samplesperpixel == 1


def test_a_level_beneath_the_cache_reads_the_stores_and_writes_the_same_file(tma, monkeypatch):
    from clinicalomx.extract.builders import mosaic as mosaic_mod

    cached, _ = tma(n=6, pitch=PYRAMID_PITCH, tag="a")
    monkeypatch.setattr(mosaic_mod, "PYRAMID_TOP_BYTES", 1)
    reread, _ = tma(n=6, pitch=PYRAMID_PITCH, tag="b")
    with tifffile.TiffFile(cached) as a, tifffile.TiffFile(reread) as b:
        for left, right in zip(a.series, b.series, strict=True):
            assert len(left.levels) == len(right.levels) >= 4
            for index, (x, y) in enumerate(zip(left.levels, right.levels, strict=True)):
                assert (x.asarray() == y.asarray()).all(), f"{left.name} level {index} differs"
                assert ([c for pg in x.pages for c in pg.databytecounts if c == 0]
                        == [c for pg in y.pages for c in pg.databytecounts if c == 0]), (
                    f"{left.name} level {index} stores a different set of tiles")


def _tiles_the_cores_touch(bounds, boxes, factor, tile=(512, 512)):
    """How many grid cells the cores reach at ``factor``, per channel. Independent arithmetic."""
    x0, y0, x1, y1 = bounds
    height = max(1, -(-(y1 - y0) // factor))
    width = max(1, -(-(x1 - x0) // factor))
    rects = [((int(b1) - y0) // factor, (int(b0) - x0) // factor,
              (int(b1) - y0) // factor + -(-(int(b3) - int(b1)) // factor),
              (int(b0) - x0) // factor + -(-(int(b2) - int(b0)) // factor))
             for b0, b1, b2, b3 in boxes]
    th, tw = tile
    grid = touched = 0
    for top_y in range(0, height, th):
        bottom_y = min(top_y + th, height)
        for left_x in range(0, width, tw):
            right_x = min(left_x + tw, width)
            grid += 1
            if any(top < bottom_y and bottom > top_y and left < right_x and right > left_x
                   for top, left, bottom, right in rects):
                touched += 1
    return grid, touched


def test_every_level_stores_the_tiles_its_cores_touch_and_no_others(tma):
    path, _detail = tma(n=6, pitch=PYRAMID_PITCH)
    boxes = [crop_box(_bounds(i, PYRAMID_PITCH)) for i in range(6)]
    bounds = union_bounds(boxes)
    factors = pyramid_levels(bounds)
    saw_a_gap = False
    with tifffile.TiffFile(path) as handle:
        for one in handle.series:
            for index, level in enumerate(one.levels):
                counts = [c for page in level.pages for c in page.databytecounts]
                grid, touched = _tiles_the_cores_touch(bounds, boxes, factors[index])
                channels = len(level.pages)
                expected = touched * channels + (0 if touched else 1)
                assert len(counts) == grid * channels, f"{one.name} level {index} grid moved"
                assert len(counts) - counts.count(0) == expected, (
                    f"{one.name} level {index} stored {len(counts) - counts.count(0)} tiles where "
                    f"its cores touch {expected}")
                saw_a_gap = saw_a_gap or (grid > touched)
    assert saw_a_gap, "the fixture has no empty tile at any level, so it proves nothing"


def test_the_base_level_is_byte_identical_to_a_file_with_no_pyramid(tmp_path):
    from clinicalomx.extract.builders.imaging import ChannelMeta
    from clinicalomx.extract.builders.mosaic import overview_sources
    from clinicalomx.extract.builders.ometiff import to_ome, write_ome_tiff

    cores = [_core(f"c{i}", i + 1, x=i * 512, y=0, size=200, channels=2) for i in (0, 1, 3)]
    bounds = (0, 0, 4 * 512, 512)

    def _one(**kw):
        return _mosaic(cores, bounds, tile=(512, 512), **kw)

    flat = tmp_path / "flat.ome.tiff"
    series = [("m", _one(), "uint16")]
    write_ome_tiff(series, to_ome(series, ChannelMeta()), flat)

    deep = tmp_path / "deep.ome.tiff"
    # `decimate` is the stride on each level's read: 2 at the first overview, 4 // 2 above it.
    levels = tuple(_mosaic(sources, bounds, tile=(512, 512), downsample=step, decimate=decimate)
                   for step, (sources, decimate) in zip(
                       (2, 4), overview_sources(tuple(cores), (1, 2, 4), 2), strict=True))
    series = [("m", _one(), "uint16")]
    write_ome_tiff(series, to_ome(series, ChannelMeta()), deep, pyramid=lambda _si: levels)

    with tifffile.TiffFile(flat) as a, tifffile.TiffFile(deep) as b:
        assert len(b.series[0].levels) == 3, "the pyramid was not written"
        assert len(a.series[0].levels) == 1, "the reference file is not flat"
        for left, right in zip(a.series[0].pages, b.series[0].levels[0].pages, strict=True):
            assert left.shape == right.shape and left.dtype == right.dtype
            assert list(left.databytecounts) == list(right.databytecounts)
            assert _tile_bytes(flat, left) == _tile_bytes(deep, right)
        assert (a.series[0].asarray() == b.series[0].levels[0].asarray()).all()


def test_an_overview_level_is_the_base_decimated_and_still_registered(tma):
    path, _detail = tma(n=6, pitch=PYRAMID_PITCH)
    with tifffile.TiffFile(path) as handle:
        one = handle.series[1]           # bgnorm: each core holds its index + 1
        base = one.levels[0].asarray()
        half = one.levels[1].asarray()
    assert half.shape[0] == base.shape[0], "an overview level lost a channel"
    for core in range(6):
        found = np.unique(base[0][base[0] == float(core + 1)])
        assert found.size, f"core {core} is not in the base"
        ys, xs = np.nonzero(base[0] == float(core + 1))
        y, x = int(ys.mean()) // 2, int(xs.mean()) // 2
        assert half[0, y, x] == float(core + 1), (
            f"core {core} is not at its own place in the overview level")


def test_the_pyramid_is_a_small_fraction_of_the_base():
    for side, pitch, channels, dtype, ceiling in ((5632, 9886, 64, "uint16", 0.50),
                                                  (2048, 6000, 64, "uint16", 0.60)):
        boxes = _grid(18, side, across=5, pitch=pitch)
        bounds = union_bounds(boxes)
        factors = pyramid_levels(bounds)
        per = [stored_bytes(boxes, dtype, channels=channels, downsample=d, bounds=bounds)
               for d in factors]
        over = sum(per[1:])
        assert 0 < over <= per[0] * ceiling, (
            f"{side} px cores: the overviews are {over} B against a {per[0]} B base")
        assert per == sorted(per, reverse=True), "a level costs more than the one below it"


def _breaks(monkeypatch, series, core, *, after):
    """Make one core's reader fail from its ``after``-th call on.

    ``after=CH`` passes the base pass and fails the first overview.
    """
    from dataclasses import replace

    def place(*a, **k):
        got = _REAL_PLACE(*a, **k)
        if got.name != series:
            return got
        out = []
        for placed in got.sources:
            if placed.name != core:
                out.append(placed)
                continue
            seen = [0]
            def read(channel, _p=placed, _seen=seen):
                _seen[0] += 1
                if _seen[0] > after:
                    raise OSError("the store went away")
                return _p.read(channel)
            out.append(replace(placed, read=read))
        return replace(got, sources=tuple(out))

    monkeypatch.setattr(ometiff, "_place", place)


def _levels_holding(path, si, value):
    """Which levels of series ``si`` carry ``value`` anywhere in channel 0."""
    with tifffile.TiffFile(path) as handle:
        return [i for i, level in enumerate(handle.series[si].levels)
                if bool((level.asarray()[0] == value).any())]


def test_a_core_that_reads_at_the_base_and_not_at_the_overviews_is_named(tma, monkeypatch):
    _breaks(monkeypatch, "bgnorm", "R0C2", after=CH)
    path, detail = tma(n=6, pitch=PYRAMID_PITCH)

    assert "PYRAMID GAP" in detail and "bgnorm/R0C2" in detail
    assert detail.count("bgnorm/R0C2") == 1, "one core dropped once per level is reported once"
    assert _levels_holding(path, 1, 3.0) == [0], (
        "the core must be in the base level and in no overview level")
    assert _levels_holding(path, 0, 0.0), "the other series must be untouched"


def test_a_core_that_fails_at_the_base_takes_the_whole_file_with_it(tma, monkeypatch, tmp_path):
    _breaks(monkeypatch, "bgnorm", "R0C2", after=0)
    with pytest.raises(ValueError, match="R0C2"):
        tma(n=6, pitch=PYRAMID_PITCH)
    assert not list(tmp_path.glob("*.ome.tiff")), "a file short a core must not be left on disk"


def test_two_series_on_different_chains_each_report_their_own_levels(tma, monkeypatch):
    from ome_types import from_xml

    from clinicalomx.extract.builders import mosaic as mosaic_mod

    real = mosaic_mod.pyramid_levels
    given = []

    def levels(bounds, *, downsample=1):
        got = real(bounds, downsample=downsample)
        got = got if not given else got[:-1]
        given.append(got)
        return got

    monkeypatch.setattr(mosaic_mod, "pyramid_levels", levels)
    path, detail = tma(n=6, pitch=PYRAMID_PITCH)

    chains = {name: list(chain[1:])
              for name, chain in zip(("raw", "bgnorm", "bgnormQ"), given, strict=True)}
    assert len({tuple(c) for c in chains.values()}) > 1, "the fixture did not diverge"

    with tifffile.TiffFile(path) as handle:
        written = {one.name: len(one.levels) - 1 for one in handle.series}
        described = {im.name: im.description for im in from_xml(handle.pages[0].description).images}
    for name, chain in chains.items():
        assert written[name] == len(chain), f"{name} wrote {written[name]} overview level(s)"
        assert f"SubIFD overview levels at {chain}" in described[name], (
            f"{name} is described as {described[name]!r} against its own chain {chain}")
        assert f"{name} at {chain}" in detail, f"the detail line does not carry {name}'s own chain"


def test_the_detail_line_reports_what_the_pyramid_cost(tma):
    _path, detail = tma(n=6, pitch=PYRAMID_PITCH)
    assert "PYRAMID" in detail and "% of the base" in detail


def test_the_layers_own_reader_still_opens_a_pyramidal_mosaic(tma):
    from clinicalomx.extract.builders.imaging import open_image

    path, _detail = tma(n=6, pitch=PYRAMID_PITCH)
    with tifffile.TiffFile(path) as handle:
        assert len(handle.series[0].levels) > 1, "the fixture is not pyramidal"
    opened = open_image(path)
    assert opened.array.shape[-2:] == tifffile.TiffFile(path).series[0].levels[0].shape[-2:]


def test_the_overview_cache_holds_one_factor_for_one_series_inside_the_budget(tma, monkeypatch):
    from clinicalomx.extract.builders.mosaic import (
        PYRAMID_TOP_BYTES,
        cache_bytes,
        cache_factor,
        pyramid_levels,
    )

    held = _cache_watch(monkeypatch)
    tma(n=6, pitch=PYRAMID_PITCH)

    boxes = [crop_box(_bounds(i, PYRAMID_PITCH)) for i in range(6)]
    bounds = union_bounds(boxes)
    factors = pyramid_levels(bounds)
    assert len(factors) > 2, "the fixture has no level above the first overview"
    cache = cache_factor(factors, boxes, "float32", channels=CH)
    one_series = cache_bytes(boxes, "float32", channels=CH, downsample=cache)
    deeper = sum(cache_bytes(boxes, "float32", channels=CH, downsample=d)
                 for d in factors if d > cache)

    assert held[0] > 0, "the cache was never observed, so this asserts nothing"
    assert held[0] <= one_series, (
        f"the caches held {held[0]} B against one series' {one_series} B at factor {cache} - "
        f"a level per level would add {deeper} B and a cache per series would multiply by 3")
    assert held[0] <= PYRAMID_TOP_BYTES, f"the cache is {held[0]} B, over the budget"


def test_the_cache_holds_the_decimation_and_not_the_source_plane():
    import weakref

    from clinicalomx.extract.builders.mosaic import overview_sources

    source = np.ones((2048, 2048), "uint16")
    gone = weakref.ref(source)
    placed = Placed("c0", (0, 0, 2048, 2048), lambda _c, a=[source]: a.pop())
    ((first, decimate), *_rest) = overview_sources((placed,), (1, 2, 4, 8), 8)
    plane = first[0].read(0)
    del source
    assert plane.shape == (2048, 2048) and decimate == 2, (
        "the first overview strides the store's own plane, as the base does")
    del plane
    assert gone() is None, "the cache is holding the full-resolution plane behind a view"
    assert first[0].read.__self__.planes[0].nbytes == 256 * 256 * 2


def test_a_store_recording_no_origin_is_placed_from_the_box_that_produced_it(tma):
    path, detail = tma(n=6, no_origin=range(6))
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    for si, value in ((1, lambda i: float(i + 1)), (2, lambda i: float(100 + i))):
        arr = tifffile.imread(path, series=si)
        for i in range(6):
            ux0, uy0, ux1, uy1 = _bounds(i)
            got = arr[0, int((uy0 + uy1) // 2) - y0, int((ux0 + ux1) // 2) - x0]
            assert got == value(i), (
                f"series {si} core {i} reads {got} at its own slide centre, not {value(i)}")
    assert "placed cores per series: raw 6 of 6 at 3c; bgnorm 6 of 6 at 3c; " in detail


#: A slide whose right and bottom edges cut cores 2 to 5 short at the default pitch.
CLIPPED_SCAN = (SLIDE_Y + PITCH + 60, SLIDE_X + 2 * PITCH + 80)


def test_a_core_cut_short_at_the_edge_of_the_scan_still_lands_at_its_own_box(tma):
    path, detail = tma(n=6, no_origin=range(6), scan=CLIPPED_SCAN)
    x0, y0, _x1, _y1 = union_bounds([crop_box(_bounds(i)) for i in range(6)])
    for si, value in ((1, lambda i: float(i + 1)), (2, lambda i: float(100 + i))):
        arr = tifffile.imread(path, series=si)
        for i in range(6):
            ux0, uy0, ux1, uy1 = _bounds(i)
            got = arr[0, int((uy0 + uy1) // 2) - y0, int((ux0 + ux1) // 2) - x0]
            assert got == value(i), (
                f"series {si} core {i} reads {got} at its own slide centre, not {value(i)}")
    assert "placed cores per series: raw 6 of 6 at 3c; bgnorm 6 of 6 at 3c; " in detail


def test_a_cut_store_that_is_not_this_crop_is_still_refused(tma):
    with pytest.raises(ValueError) as caught:
        tma(n=6, no_origin=range(6), unpadded=(5,), scan=CLIPPED_SCAN)
    said = str(caught.value)
    assert "R0C5" in said, "the unplaceable core is not named"
    assert "records no origin, and whose extent does not match this crop either" in said
    assert "A frame applied twice, or not at all, looks exactly like this." in said


def test_an_inferred_frame_is_the_same_frame_the_store_would_have_recorded(tma):
    recorded, _ = tma(n=6, tag="rec")
    derived, _ = tma(n=6, no_origin=range(6), tag="der")
    for si in range(3):
        want, got = tifffile.imread(recorded, series=si), tifffile.imread(derived, series=si)
        assert got.shape == want.shape, f"series {si} is not even the same canvas"
        assert np.array_equal(got, want), (
            f"series {si} differs: the derived frame is not the recorded one")


def test_a_store_that_is_not_this_crop_gets_no_frame_and_says_so(tma):
    with pytest.raises(ValueError) as caught:
        tma(n=6, unpadded=(3,))
    said = str(caught.value)
    assert "R0C3" in said, "the unplaceable core is not named"
    assert "records no origin, and whose extent does not match this crop either" in said
    assert "A frame applied twice, or not at all, looks exactly like this." in said


def test_a_whole_slide_source_is_still_read_in_the_slide_frame(tma, tmp_path):
    from clinicalomx.extract.builders.imaging import PARENT, open_image, resolve_origin

    path, _detail = tma(n=6, no_origin=range(6))
    slide = open_image(tmp_path / "slide.tiff")
    for i in range(6):
        assert resolve_origin(slide, _bounds(i)) == (0, 0, PARENT), (
            f"the whole slide was given core {i}'s frame, so raw is rebased twice over")
    assert tifffile.imread(path, series=0).shape[1:] == tifffile.imread(
        path, series=1).shape[1:], "raw and bgnorm are no longer one canvas"


def test_a_declared_frame_is_named_in_the_detail_rather_than_inferred_silently(tma):
    _path, declared = tma(n=6, no_origin=range(6))
    assert "FRAME DECLARED for bgnorm 6, bgnormQ 6 core(s)" in declared
    _path, recorded = tma(n=6, tag="rec")
    assert "FRAME DECLARED" not in recorded, "a recording store was reported as declared"
