"""Lazy image readers and the crop that materialises pixels.

Every reader returns a lazy array; ``.compute()`` happens only in :func:`crop`. A zarr variable is
read only when named: ``probs`` is larger than the image it describes.

``ChannelMeta.px_um`` is microns per pixel. It is unrelated to a GeoParquet's ``to_fullres``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from clinicalomx.extract.coords import PAD_RATIO, crop_box

#: Most datatree handles cached before the oldest is dropped.
_MAX_HANDLES = 8

#: Lazy datatree handles, keyed by artifact key, so cores of one slide share one handle.
_HANDLES: dict[str, Any] = {}


def reset_cache() -> None:
    """Drop the datatree handles. For tests, and for a long-lived process between jobs."""
    _HANDLES.clear()


@dataclass(frozen=True)
class ChannelMeta:
    """Channel names, colours and pixel size, read from the image's own coords where present."""

    names: tuple[str, ...] = ()
    #: ``"#rrggbb"`` per channel, as the reader writes it.
    colors: tuple[str | None, ...] = ()
    emission_nm: tuple[float | None, ...] = ()
    fluorophore: tuple[str | None, ...] = ()
    #: True where the channel is a spectrally unmixed component, not an acquisition channel.
    unmixed: tuple[bool, ...] = ()
    #: Microns per pixel.
    px_um: float | None = None
    source: str = ""

    @property
    def n(self) -> int:
        return len(self.names)

    def to_dict(self) -> dict:
        return {"names": list(self.names), "colors": list(self.colors),
                "emission_nm": list(self.emission_nm), "fluorophore": list(self.fluorophore),
                "unmixed": list(self.unmixed), "px_um": self.px_um, "source": self.source}


@dataclass(frozen=True)
class Origin:
    """Where a cropped image came from, in full-resolution pixels of its parent.

    Measured from the image's own spatial coords, so it outranks any metadata.
    """

    x: float
    y: float
    width: int
    height: int
    #: Microns per pixel.
    px_um: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height,
                "px_um": self.px_um}


@dataclass
class Opened:
    """A lazy ``(c, y, x)`` array plus what its axes mean."""

    array: Any
    meta: ChannelMeta
    #: ``(height, width)`` of the full array.
    shape_yx: tuple[int, int] = (0, 0)
    variables: tuple[str, ...] = field(default_factory=tuple)
    #: Recorded crop origin. ``None`` means no evidence, never origin zero.
    origin: Origin | None = None


@dataclass(frozen=True)
class ArrayInfo:
    """One array inside a store, described from its metadata and never from its contents."""

    name: str
    dims: tuple[str, ...]
    shape: tuple[int, ...]
    dtype: str
    #: A short coordinate's values. Always empty for a data variable.
    values: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "dims": list(self.dims), "shape": list(self.shape),
                "dtype": self.dtype, "values": list(self.values)}


@dataclass(frozen=True)
class StoreContents:
    """What one artifact holds. A failure is the ``error`` field, not an exception."""

    key: str
    variables: tuple[ArrayInfo, ...] = ()
    coords: tuple[ArrayInfo, ...] = ()
    #: Why nothing could be listed. Empty on success.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {"key": self.key, "error": self.error,
                "variables": [v.to_dict() for v in self.variables],
                "coords": [c.to_dict() for c in self.coords]}


#: Longest coordinate whose values are read. ``y`` and ``x`` are always longer.
_MAX_COORD_VALUES = 64


def _array_info(arr, name: str, *, read_values: bool) -> ArrayInfo:
    values: tuple[str, ...] = ()
    if read_values and int(arr.size) <= _MAX_COORD_VALUES:
        try:
            values = tuple(str(v) for v in arr.values.ravel())
        except Exception:  # noqa: BLE001 - an unreadable coord is absent metadata, not a failure
            values = ()
    return ArrayInfo(
        name=name,
        dims=tuple(str(d) for d in arr.dims),
        shape=tuple(int(n) for n in arr.shape),
        dtype=str(arr.dtype),
        values=values,
    )


def describe_store(artifact, *, key: str = "") -> StoreContents:
    """Every array in a store, from its metadata. Never reads a data variable.

    Only coordinate values up to :data:`_MAX_COORD_VALUES` long are read.
    """
    path = getattr(artifact, "path", artifact)
    name = key or str(path)
    if not str(path).rstrip("/").lower().endswith(".zarr"):
        return StoreContents(name, error=(
            "not a zarr, so its arrays are not listable from metadata alone. A qptiff's layout "
            "needs the pyramid reader, which is a different and much heavier read."))
    try:
        ds = _open_dataset(path)
    except ImportError as exc:
        return StoreContents(name, error=(
            f"this environment has no zarr reader ({exc}). Listing a store's arrays needs "
            f"`clinicalomx[extract]`; planning never does, which is why it is not installed "
            f"everywhere."))
    except Exception as exc:  # noqa: BLE001 - an unreadable store is a reported state, not a crash
        return StoreContents(name, error=f"{type(exc).__name__}: {exc}")
    return StoreContents(
        key=name,
        variables=tuple(_array_info(ds[v], str(v), read_values=False) for v in ds.data_vars),
        coords=tuple(_array_info(ds.coords[c], str(c), read_values=True) for c in ds.coords),
    )


def _coord(ds, name):
    try:
        return list(ds.coords[name].values) if name in ds.coords else None
    except Exception:  # noqa: BLE001 - a malformed coord is absent metadata, not a failure
        return None


def _is_default_index(values) -> bool:
    """Is this the ``0..n-1`` index xarray fabricates for a dimension with no stored coord?"""
    import numpy as np

    return bool(np.issubdtype(values.dtype, np.integer)
                and np.array_equal(values, np.arange(len(values))))


def _origin_from(arr, meta: ChannelMeta) -> Origin | None:
    """The recorded crop origin, from the array's own spatial coords. ``None`` if absent.

    The coord spacing is the pixel size, so ``first / step`` is the offset in pixels. A declared
    ``px_um`` that disagrees with the spacing gives ``None``. Default integer indices are ignored.
    """
    try:
        y = arr.coords["y"].values
        x = arr.coords["x"].values
    except (KeyError, AttributeError, TypeError):
        return None
    if len(y) < 2 or len(x) < 2:
        return None   # a single row proves nothing about the spacing
    if _is_default_index(y) or _is_default_index(x):
        return None

    try:
        dy, dx = float(y[1]) - float(y[0]), float(x[1]) - float(x[0])
        if dy <= 0 or dx <= 0:
            return None
        if meta.px_um and not (math.isclose(dy, meta.px_um, rel_tol=1e-3)
                               and math.isclose(dx, meta.px_um, rel_tol=1e-3)):
            return None
        return Origin(x=float(x[0]) / dx, y=float(y[0]) / dy,
                      width=int(len(x)), height=int(len(y)), px_um=dx)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _channel_meta_from(ds, arr, *, source: str) -> ChannelMeta:
    """Channel metadata from an xarray object's coords and attrs. Every field is optional."""
    names = _coord(ds, "c") or []
    n = len(names)

    def per_channel(key, cast):
        raw = _coord(ds, key)
        if raw is None or len(raw) != n:
            return tuple([None] * n)
        out = []
        for v in raw:
            try:
                out.append(None if v is None else cast(v))
            except (TypeError, ValueError):
                out.append(None)
        return tuple(out)

    px_um = None
    info = (getattr(arr, "attrs", {}) or {}).get("image_info")
    if isinstance(info, dict) and info.get("scale_factor") is not None:
        try:
            # Microns per pixel (<PixelSizeMicrons> in the qptiff XML).
            px_um = float(info["scale_factor"])
        except (TypeError, ValueError):
            px_um = None

    return ChannelMeta(
        names=tuple(str(v) for v in names),
        colors=per_channel("color_rgb", str),
        emission_nm=per_channel("emission_wavelength_nm", float),
        fluorophore=per_channel("fluorophore", str),
        unmixed=tuple(bool(v) for v in (_coord(ds, "is_unmixed_component") or [False] * n)),
        px_um=px_um,
        source=source,
    )


def _open_dataset(path):
    """A dask-backed Dataset, flat or hierarchical. Never numpy-backed."""
    import xarray as xr

    try:
        return xr.open_zarr(str(path))
    except Exception:  # noqa: BLE001 - a hierarchical store rather than a flat Dataset
        return xr.open_datatree(str(path), chunks={}).to_dataset()


def _open_zarr(path, variable: str | None) -> Opened:
    ds = _open_dataset(path)

    available = tuple(str(v) for v in ds.data_vars)
    if variable is None:
        raise LookupError(
            f"{path}: no variable named. It holds {available}; the recipe's Element must say "
            f"which one. Guessing here risks `probs`, which is larger than the image it "
            f"describes, and would silently pick the wrong array the day a producer adds one.")
    if variable not in available:
        raise LookupError(f"{path}: no variable {variable!r}; it holds {available}")

    arr = ds[variable]
    dims = {str(d).lower(): str(d) for d in arr.dims}
    h = int(arr.sizes[dims["y"]]) if "y" in dims else 0
    w = int(arr.sizes[dims["x"]]) if "x" in dims else 0
    meta = _channel_meta_from(ds, arr, source=f"zarr:{variable}")
    return Opened(arr, meta, (h, w), available, origin=_origin_from(arr, meta))


def _open_tifffile(path) -> Opened:
    """A plain TIFF, lazily, without bioio. Level 0 only, with no channel metadata."""
    import dask.array as da
    import tifffile
    import xarray as xr

    store = tifffile.imread(str(path), aszarr=True, level=0)
    arr = da.from_zarr(store)
    if arr.ndim == 2:
        arr = arr[None, ...]
    dims = ("c", "y", "x")[-arr.ndim:]
    wrapped = xr.DataArray(arr, dims=dims)
    h = int(wrapped.sizes["y"]) if "y" in wrapped.dims else 0
    w = int(wrapped.sizes["x"]) if "x" in wrapped.dims else 0
    return Opened(wrapped, ChannelMeta(source="tifffile"), (h, w), ("image",))


def _open_bioio(path) -> Opened:
    """The largest image-bearing level of a qptiff's FullResolution scene, lazily.

    Needs ``bioio`` and ``bioio_qptiff``, which are not declared dependencies. Without them this
    reads level 0 through tifffile.
    """
    try:
        from bioio import BioImage
        from bioio_qptiff import Reader
    except ImportError:
        return _open_tifffile(path)

    key = str(path)
    if key not in _HANDLES:
        if len(_HANDLES) >= _MAX_HANDLES:
            _HANDLES.pop(next(iter(_HANDLES)))   # oldest out
        _HANDLES[key] = BioImage(key, reader=Reader).reader.xarray_dask_scene_datatree[
            "FullResolution"]
    tree = _HANDLES[key]

    nodes = [n for n in tree.subtree if "image" in n.ds.variables]
    if not nodes:
        raise LookupError(f"{path}: the scene datatree has no image-bearing level")
    node = max(nodes, key=lambda n: n.ds["image"].size)
    arr = node.ds["image"]

    dims = {str(d).lower(): str(d) for d in arr.dims}
    h = int(arr.sizes[dims["y"]]) if "y" in dims else 0
    w = int(arr.sizes[dims["x"]]) if "x" in dims else 0
    return Opened(arr, _channel_meta_from(node.ds, arr, source="qptiff"), (h, w), ("image",))


def open_image(artifact, *, variable: str | None = None) -> Opened:
    """A lazy ``(c, y, x)`` array plus its channel metadata, chosen by suffix. Loads no pixel."""
    path = getattr(artifact, "path", artifact)
    name = str(path).rstrip("/").lower()
    if name.endswith(".zarr"):
        return _open_zarr(path, variable)
    if variable is not None:
        raise LookupError(
            f"{path} is not a zarr, so it has no variable {variable!r} to select. Drop `variable` "
            f"from the Element, or point it at the bgnormed zarr.")
    return _open_bioio(path)


def as_cyx(array):
    """Normalise to ``(c, y, x)``. A 2D plane is one channel; an RGB scan arrives channel-last."""
    import numpy as np

    if array.ndim == 2:
        return array[np.newaxis, ...]
    if array.ndim == 3 and array.shape[-1] in (3, 4) and array.shape[0] > 4:
        return np.moveaxis(array, -1, 0)
    return array


def _axes(opened: Opened) -> tuple[list[str], set[str], list[str]]:
    """``(dims, the y/x pair, everything else)``."""
    dims = [str(d) for d in getattr(opened.array, "dims", ())]
    spatial = {d for d in dims if d.lower() in ("y", "x")}
    return dims, spatial, [d for d in dims if d not in spatial]


def channels_of(opened: Opened) -> tuple[int, str]:
    """``(channel count, "")`` from the axes alone, or ``(0, why not)``. Reads no pixel."""
    dims, spatial, other = _axes(opened)
    if len(spatial) != 2:
        return 0, f"its axes are {tuple(dims)}, which has no y/x pair to crop"
    if len(other) > 1:
        return 0, (f"its axes are {tuple(dims)}: more than a channel axis beside y/x, so it is "
                   f"not a (c, y, x) image")
    return (int(opened.array.sizes[other[0]]) if other else 1), ""


def channel_views(opened: Opened):
    """Yield one :class:`Opened` per channel, sharing this store's frame. Reads no pixel.

    Every view keeps ``shape_yx`` and ``origin``, so each resolves the same crop box. A source with
    no single channel axis yields ``opened`` itself, once.
    """
    _dims, spatial, other = _axes(opened)
    if len(spatial) != 2 or len(other) != 1:
        yield opened
        return
    for i in range(int(opened.array.sizes[other[0]])):
        yield channel_view(opened, i)


def channel_view(opened: Opened, channel: int) -> Opened:
    """One channel of this store, sharing its frame. Reads no pixel.

    Returns ``opened`` itself when the axes do not separate; index the channel out of the crop.
    """
    _dims, spatial, other = _axes(opened)
    if len(spatial) != 2 or len(other) != 1:
        return opened
    # A scalar `isel` drops the channel dim; `as_cyx` restores it as length 1.
    return replace(opened, array=opened.array.isel({other[0]: channel}))


def _expected(bounds, pad_ratio, parent_shape):
    """``(padded box, unpadded box)`` in the parent's frame, clamped to ``parent_shape`` if given.

    The padded box matches what ``bgnorm_stage._crop_full_res`` cut.
    """
    ph, pw = parent_shape if parent_shape else (None, None)
    padded = crop_box(bounds, pad_ratio=PAD_RATIO if pad_ratio is None else pad_ratio,
                      width=int(pw) if pw else None, height=int(ph) if ph else None)
    ux0, uy0 = int(math.floor(float(bounds[0]))), int(math.floor(float(bounds[1])))
    ux1, uy1 = int(math.ceil(float(bounds[2]))), int(math.ceil(float(bounds[3])))
    if pw:
        ux0, ux1 = max(0, ux0), min(int(pw), ux1)
    if ph:
        uy0, uy1 = max(0, uy0), min(int(ph), uy1)
    return padded, (ux0, uy0, ux1, uy1)


#: How :func:`resolve_origin` established a store's frame.
RECORDED = "recorded"
DECLARED = "declared"
PARENT = "parent"


def resolve_origin(opened: Opened, bounds, *, pad_ratio=None,
                   parent_shape=None) -> tuple[int, int, str]:
    """Where this store's ``(0, 0)`` sits in the frame of ``bounds``, as ``(x, y, how)``.

    ``how`` is ``RECORDED`` (the store's own coords), ``DECLARED`` (its extent matches the padded
    crop of ``bounds`` and covers the unpadded box) or ``PARENT`` (read in the box's own frame).
    ``parent_shape`` is ``(h, w)`` of the scan ``bounds`` is measured in. Reads no pixel.
    """
    if opened.origin is not None:
        # Rounded, not floored: the value is a micron coord divided by the pixel size.
        return int(round(opened.origin.x)), int(round(opened.origin.y)), RECORDED
    h, w = opened.shape_yx
    if not (w and h):
        return 0, 0, PARENT
    (x0, y0, x1, y1), (ux0, uy0, ux1, uy1) = _expected(bounds, pad_ratio, parent_shape)
    if w > x1 - x0 or h > y1 - y0:
        return 0, 0, PARENT     # larger than this crop: the parent itself
    ox, oy = max(0, x0), max(0, y0)
    if ox <= ux0 and oy <= uy0 and ox + w >= ux1 and oy + h >= uy1:
        return ox, oy, DECLARED
    return 0, 0, PARENT


class Frame(NamedTuple):
    """A crop box in the parent's frame plus the store origin, from one :func:`resolve_origin`."""

    box: tuple[int, int, int, int]
    ox: int
    oy: int
    how: str


def crop_bounds(opened: Opened, bounds, *, pad_ratio=None, parent_shape=None) -> Frame:
    """The box :func:`crop` will take, in the parent's frame, clamped to this store. Reads nothing.

    Raises ``ValueError`` naming the frame that applied when the box misses the store.
    """
    h, w = opened.shape_yx
    ox, oy, how = resolve_origin(opened, bounds, pad_ratio=pad_ratio, parent_shape=parent_shape)
    (x0, y0, x1, y1), _unpadded = _expected(bounds, pad_ratio, parent_shape)
    ew, eh = x1 - x0, y1 - y0
    if w:
        x0, x1 = max(ox, x0), min(ox + w, x1)
    if h:
        y0, y1 = max(oy, y0), min(oy + h, y1)
    if x1 <= x0 or y1 <= y0:
        if parent_shape:
            ph, pw = parent_shape
            unmatched = (f"which records no origin, and whose extent does not match this crop "
                         f"either ({ew}x{eh} expected, the padded box cut to the {pw}x{ph} scan), "
                         f"so it was read in the same frame as the box")
        else:
            unmatched = (f"which records no origin, and whose extent does not match this crop "
                         f"either ({ew}x{eh} expected, and this caller named no parent extent, so "
                         f"a store cut short at the edge of the scan reads exactly like one "
                         f"written at another box), so it was read in the same frame as the box")
        where = {
            RECORDED: f"which records its origin at ({ox}, {oy})",
            DECLARED: f"which was placed at ({ox}, {oy}) from the box that produced it",
            PARENT: unmatched,
        }[how]
        raise ValueError(
            f"the crop box ({x0}, {y0}, {x1}, {y1}) falls outside the {w}x{h} source, {where}. A "
            f"frame applied twice, or not at all, looks exactly like this.")
    return Frame((int(x0), int(y0), int(x1), int(y1)), ox, oy, how)


def crop(opened: Opened, bounds, *, pad_ratio=None, parent_shape=None):
    """Materialise the padded crop of ``bounds``, and only that.

    ``bounds`` is in the parent's full-resolution pixels. Returns ``(c, y, x)`` ndarray and the box
    in the parent's frame. This is the only ``.compute()`` in the layer.
    """
    import numpy as np

    (x0, y0, x1, y1), ox, oy, _how = crop_bounds(opened, bounds, pad_ratio=pad_ratio,
                                                 parent_shape=parent_shape)

    arr = opened.array
    dims = {str(d).lower(): str(d) for d in getattr(arr, "dims", ())}
    ys, xs = slice(y0 - oy, y1 - oy), slice(x0 - ox, x1 - ox)
    if "y" in dims and "x" in dims:
        arr = arr.isel({dims["y"]: ys, dims["x"]: xs})
        out = np.asarray(arr.compute() if hasattr(arr, "compute") else arr)
    else:
        out = np.asarray(arr)[..., ys, xs]
    return as_cyx(out), (x0, y0, x1, y1)
