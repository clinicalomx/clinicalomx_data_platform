"""Core crops placed on one canvas in the parent image's frame, as a sparse tile grid.

The canvas is the union of the core boxes, offset from the slide origin. A tile no core covers is
yielded as ``None``, which ``tifffile`` writes as absent and reads back as zeros. Cores are read
one channel at a time. A core that fails after channel 0 abandons the whole series. Overlaps keep
the first write and are reported. Overview levels are sparse too, at every power of two, and cost
one extra pass over the pixels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Refuse a mosaic that would store more than this many bytes per series, over every level.
MAX_STORED_BYTES = 256 * 1024**3

#: Tile edge in pixels. A tile is the unit of absence.
TILE = (512, 512)

#: Stop adding overview levels once the longest edge is at or under this many pixels.
PYRAMID_FLOOR = 1024

#: Memory budget in bytes for the decimated cores held while overviews are written. It sets the
#: cache factor, not which levels exist.
PYRAMID_TOP_BYTES = 16 * 1024**3


class MosaicError(RuntimeError):
    """A mosaic could not be built. The message carries the number that made it impossible."""


class EmptyMosaic(MosaicError):
    """No core could supply this series. Other series may still be fine."""


@dataclass(frozen=True)
class Placed:
    """One core's place on the canvas, and how to read one of its planes.

    ``box`` is in full-resolution parent pixels, clamped to the store. ``read(channel)`` returns a
    ``(y, x)`` array covering ``box``.
    """

    name: str
    box: tuple
    read: object


@dataclass
class Sparse:
    """A mosaic as a tile producer. Shape and dtype are fixed before any pixel is read.

    The report fields after ``downsample`` fill only as :meth:`tiles` is consumed.
    """

    shape: tuple
    dtype: object
    #: Where the canvas's (0, 0) sits in the parent image, in full-resolution pixels.
    origin_x: int
    origin_y: int
    #: The cores, in overlap-priority order.
    sources: tuple = ()
    downsample: int = 1
    #: Stride applied to each plane a source returns, when it differs from :attr:`downsample`.
    #: ``None`` means equal to :attr:`downsample`.
    decimate: int | None = None
    tile: tuple = TILE
    placed: list = field(default_factory=list)
    #: ``(name, name)`` pairs whose padded boxes touched.
    overlaps: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    stored_tiles: int = 0
    empty_tiles: int = 0
    #: Uncompressed bytes in the written tiles.
    stored_bytes: int = 0

    @property
    def stride(self) -> int:
        return self.downsample if self.decimate is None else self.decimate

    def release(self) -> None:
        """Drop whatever the sources cached. The counters survive."""
        for placed in self.sources:
            cache = getattr(placed.read, "__self__", None)
            if isinstance(cache, _Cache):
                cache.planes.clear()

    def tiles(self):
        """Yield every tile in the writer's order, ``None`` where no core covers one. Consume once.

        A core unreadable at channel 0 is skipped; one that fails later raises :class:`MosaicError`.
        """
        return _tiles(self)


def _rects(boxes, *, origin_x, origin_y, height, width, downsample):
    """``(top, left, bottom, right)`` per box, in canvas pixels. Shared by writer and ceiling."""
    out = []
    for box in boxes:
        bx0, by0, bx1, by1 = box
        top = (int(by0) - origin_y) // downsample
        left = (int(bx0) - origin_x) // downsample
        bottom = min(top + math.ceil((int(by1) - int(by0)) / downsample), height)
        right = min(left + math.ceil((int(bx1) - int(bx0)) / downsample), width)
        out.append((top, left, bottom, right))
    return out


def _spots(sparse: Sparse, sources):
    """``(placed, top, left, bottom, right)`` per core, in canvas pixels. Reads nothing."""
    _c, height, width = sparse.shape
    rects = _rects([p.box for p in sources], origin_x=sparse.origin_x, origin_y=sparse.origin_y,
                   height=height, width=width, downsample=sparse.downsample)
    return [(placed, *rect) for placed, rect in zip(sources, rects, strict=True)]


def _tiles(sparse: Sparse):
    import numpy as np

    channels, height, width = sparse.shape
    th, tw = sparse.tile
    dtype = np.dtype(sparse.dtype)
    spots = _spots(sparse, sparse.sources)
    stride = sparse.stride
    dead: set[int] = set()

    for channel in range(channels):
        live: dict[int, object] = {}
        for top_y in range(0, height, th):
            bottom_y = min(top_y + th, height)
            for i in [i for i, plane in live.items() if spots[i][3] <= top_y]:
                del live[i]
            for i, (placed, top, _left, bottom, _right) in enumerate(spots):
                if i in live or i in dead or top >= bottom_y or bottom <= top_y:
                    continue
                try:
                    plane = np.asarray(placed.read(channel))
                except Exception as exc:  # noqa: BLE001 - any read failure, from any store
                    why = f"{placed.name} at channel {channel} ({type(exc).__name__}: {exc})"
                    sparse.skipped.append(why)
                    if channel:
                        raise MosaicError(
                            f"{why}, and its channels 0..{channel - 1} are already written. A "
                            f"core is all or nothing: the remaining channels would read back as "
                            f"zero, which nobody can tell apart from a marker that is genuinely "
                            f"absent, so this series is abandoned rather than written "
                            f"band-inconsistent.") from exc
                    dead.add(i)
                    continue
                if stride > 1:
                    # Copy, never a strided view: a view keeps the full plane alive.
                    plane = np.ascontiguousarray(plane[::stride, ::stride])
                live[i] = plane
                if placed.name not in sparse.placed:
                    sparse.placed.append(placed.name)
            for left_x in range(0, width, tw):
                tile = _one_tile(sparse, spots, live, dtype,
                                 top_y, left_x, bottom_y - top_y, min(tw, width - left_x))
                if tile is None and channel == 0 and top_y == 0 and left_x == 0:
                    # tifffile needs the first tile to be an array, not None.
                    tile = np.zeros((bottom_y - top_y, min(tw, width - left_x)), dtype)
                if tile is None:
                    sparse.empty_tiles += 1
                else:
                    sparse.stored_tiles += 1
                    sparse.stored_bytes += int(tile.nbytes)
                yield tile
                del tile
        live.clear()


def _one_tile(sparse: Sparse, spots, live, dtype, top_y, left_x, height, width):
    """One tile, or ``None`` where no core reaches it. First write wins; collisions are named."""
    import numpy as np

    out = mask = None
    # Ascending index is target order, so the first write wins deterministically.
    for i in sorted(live):
        placed, top, left, bottom, right = spots[i]
        y0, y1 = max(top_y, top), min(top_y + height, bottom)
        x0, x1 = max(left_x, left), min(left_x + width, right)
        if y1 <= y0 or x1 <= x0:
            continue
        if out is None:
            out = np.zeros((height, width), dtype)
            mask = np.zeros((height, width), bool)
        where = (slice(y0 - top_y, y1 - top_y), slice(x0 - left_x, x1 - left_x))
        fresh = ~mask[where]
        if not fresh.all():
            pair = (placed.name, _first_clash(sparse, spots, live, i, y0, y1, x0, x1))
            if pair not in sparse.overlaps:
                sparse.overlaps.append(pair)
        source = live[i][y0 - top:y1 - top, x0 - left:x1 - left]
        out[where][fresh] = source[fresh]
        mask[where] = True
    return out


def _first_clash(sparse: Sparse, spots, live, index, y0, y1, x0, x1) -> str:
    """Which earlier core already owns this region."""
    for j in sorted(live):
        if j >= index:
            break
        placed, top, left, bottom, right = spots[j]
        if top < y1 and bottom > y0 and left < x1 and right > x0:
            return placed.name
    return "an earlier core"


def pyramid_levels(bounds, *, downsample: int = 1) -> tuple[int, ...]:
    """The absolute downsample factor of every level, base first: every power of two.

    The last level is the first whose longest edge is at or under :data:`PYRAMID_FLOOR`.
    """
    _c, height, width = canvas_shape(bounds, channels=1, downsample=downsample)
    out, step = [int(downsample)], 1
    while max(math.ceil(height / step), math.ceil(width / step)) > PYRAMID_FLOOR:
        step *= 2
        out.append(int(downsample) * step)
    return tuple(out)


def cache_factor(levels, boxes, dtype, *, channels: int) -> int:
    """The finest overview factor whose cached cores fit :data:`PYRAMID_TOP_BYTES`, else the top."""
    for factor in levels[1:]:
        if cache_bytes(boxes, dtype, channels=channels, downsample=factor) <= PYRAMID_TOP_BYTES:
            return int(factor)
    return int(levels[-1])


def cache_bytes(boxes, dtype, *, channels: int, downsample: int) -> int:
    """Bytes the decimated core boxes occupy at ``downsample``."""
    import numpy as np

    pixels = sum(math.ceil((int(y1) - int(y0)) / downsample)
                 * math.ceil((int(x1) - int(x0)) / downsample) for x0, y0, x1, y1 in boxes)
    return int(pixels * channels * np.dtype(dtype).itemsize)


@dataclass
class _Cache:
    """One core's planes at the cache factor, filled by the first overview pass."""

    read: object
    factor: int
    planes: dict = field(default_factory=dict)

    def fill(self, channel):
        """The first overview level's read: the store's full plane, cached decimated on the way."""
        import numpy as np

        plane = np.asarray(self.read(channel))
        if channel not in self.planes:
            # Copy, never a strided view: a view keeps the full plane alive.
            self.planes[channel] = np.ascontiguousarray(plane[::self.factor, ::self.factor])
        return plane

    def at(self, channel):
        """A deeper level's read. Raises ``KeyError`` for a core dropped at channel 0."""
        return self.planes[channel]


def overview_sources(sources, levels, cache: int) -> tuple:
    """``(sources, decimate)`` for every overview level, base excluded.

    The first overview reads the stores and fills the cache; levels at or above ``cache`` stride
    the cache; levels in between read the stores again.
    """
    caches = tuple(_Cache(p.read, cache) for p in sources)
    out = []
    for index, step in enumerate(levels[1:]):
        if index == 0:
            reads = [c.fill for c in caches]
            out.append((_as(sources, reads), int(step)))
        elif step >= cache:
            reads = [c.at for c in caches]
            out.append((_as(sources, reads), int(step) // cache))
        else:
            out.append((tuple(sources), int(step)))
    return tuple(out)


def _as(sources, reads) -> tuple:
    return tuple(Placed(p.name, p.box, read) for p, read in zip(sources, reads, strict=True))


def union_bounds(boxes) -> tuple[int, int, int, int]:
    """The smallest box containing every core, in parent-frame pixels."""
    boxes = list(boxes)
    if not boxes:
        raise MosaicError("no cores to composite; there is nothing to make a canvas from")
    xs0, ys0, xs1, ys1 = zip(*boxes, strict=True)
    return int(min(xs0)), int(min(ys0)), int(math.ceil(max(xs1))), int(math.ceil(max(ys1)))


def canvas_shape(bounds, *, channels: int, downsample: int = 1) -> tuple[int, int, int]:
    x0, y0, x1, y1 = bounds
    return (channels,
            max(1, math.ceil((y1 - y0) / downsample)),
            max(1, math.ceil((x1 - x0) / downsample)))


def estimate_bytes(shape, dtype) -> int:
    import numpy as np

    return int(np.dtype(dtype).itemsize) * int(shape[0]) * int(shape[1]) * int(shape[2])


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _stored_areas(boxes, *, bounds=None, downsample: int = 1, tile=TILE):
    """``(covered, forced)`` canvas pixels the writer will store, counted in whole tiles.

    ``forced`` is the corner tile written when no core reaches it.
    """
    boxes = [tuple(int(v) for v in b) for b in boxes]
    if not boxes:
        return 0, 0
    bounds = union_bounds(boxes) if bounds is None else bounds
    _c, height, width = canvas_shape(bounds, channels=1, downsample=downsample)
    rects = _rects(boxes, origin_x=int(bounds[0]), origin_y=int(bounds[1]),
                   height=height, width=width, downsample=downsample)
    th, tw = tile
    covered, forced = 0, 0
    for top_y in range(0, height, th):
        bottom_y = min(top_y + th, height)
        band = [(left, right) for top, left, bottom, right in rects
                if top < bottom_y and bottom > top_y]
        for left_x in range(0, width, tw):
            right_x = min(left_x + tw, width)
            area = (bottom_y - top_y) * (right_x - left_x)
            if any(left < right_x and right > left_x for left, right in band):
                covered += area
            elif top_y == 0 and left_x == 0:
                forced = area
    return covered, forced


def stored_bytes(boxes, dtype, *, channels: int, downsample: int = 1, bounds=None) -> int:
    """Uncompressed bytes the file will hold: the tiles the cores touch, never the canvas.

    An upper bound on ``Sparse.stored_bytes``; equal when every core is read.
    """
    import numpy as np

    covered, forced = _stored_areas(boxes, bounds=bounds, downsample=downsample)
    return int((covered * channels + forced) * np.dtype(dtype).itemsize)


def check_footprint(boxes, dtype, *, channels: int, downsamples=(1,), bounds=None) -> int:
    """Bytes the file will store across every level of ``downsamples``, or raise MosaicError."""
    limit = MAX_STORED_BYTES
    boxes = list(boxes)
    factors = tuple(int(d) for d in downsamples) or (1,)
    per_level = [stored_bytes(boxes, dtype, channels=channels, downsample=d, bounds=bounds)
                 for d in factors]
    size = sum(per_level)
    if size <= limit:
        return size
    shape = canvas_shape(bounds if bounds is not None else union_bounds(boxes),
                         channels=channels, downsample=factors[0])
    canvas = estimate_bytes(shape, dtype)
    levels = (f" across {len(factors)} pyramid levels, of which the base is {_human(per_level[0])}"
              if len(factors) > 1 else "")
    needed = factors[0] * math.ceil(math.sqrt(size / limit))
    while _fits(boxes, dtype, channels, needed, bounds) > limit and needed < 1 << 20:
        needed *= 2
    raise MosaicError(
        f"this mosaic would STORE {_human(size)} of whole tiles{levels} - {len(boxes)} cores "
        f"covering {100 * per_level[0] / canvas:.0f}% of a {shape[0]} x {shape[1]} x {shape[2]} "
        f"canvas of {dtype} - over the {_human(limit)} ceiling. The ceiling counts the stored "
        f"pixels of every level and not the canvas: the gaps between the cores are written as "
        f"absent tiles and cost the file nothing, so this is the data itself. A full-resolution "
        f"TMA is genuinely this large - it is not a bug. Pass downsample>={needed} for an "
        f"overview, build `omezarr.tma.plate`, which has no canvas and no ceiling, or build the "
        f"cores separately with a per_child recipe.")


def _fits(boxes, dtype, channels, downsample, bounds) -> int:
    """What a whole pyramid based at ``downsample`` would store."""
    factors = pyramid_levels(bounds if bounds is not None else union_bounds(boxes),
                             downsample=downsample)
    return sum(stored_bytes(boxes, dtype, channels=channels, downsample=d, bounds=bounds)
               for d in factors)
