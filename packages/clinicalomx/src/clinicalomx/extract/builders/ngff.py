"""A TMA or a whole slide as an OME-NGFF 0.5 plate: one well per core, one image group per series.

There is no canvas; a plate costs the sum of its cores. Each image carries a ``scale`` (pixel size)
and a ``translation`` (the crop's offset from the slide origin), so a reader recovers the pixel
offset as ``translation / scale``. Peak memory is one channel of one core.

Spec: https://ngff.openmicroscopy.org/specifications/0.5 - Zarr v3, metadata under
``zarr.json``'s ``attributes.ome``.
"""
from __future__ import annotations

import math
import pathlib
import re
import shutil
from dataclasses import dataclass

from clinicalomx.extract.builders.imaging import channel_views, channels_of, crop, open_image
from clinicalomx.extract.builders.mosaic import _human
from clinicalomx.extract.builders.ometiff import _child_targets, _to_writable

#: The OME-Zarr version, declared once per group on the ``ome`` attribute.
NGFF_VERSION = "0.5"

#: zstd level for the pixel codec.
ZSTD_LEVEL = 5

#: Inner chunk edge, in pixels. One chunk is one channel.
CHUNK = 1024

#: Channels per shard. One shard holds one whole channel, written whole.
SHARD_CHANNELS = 1

#: ``R4C2``: the core identity, used when the dearray parquet has no row/col columns.
_RC = re.compile(r"^R(\d+)C(\d+)$")

#: Valid ``omero.channels[].color``: RGB or RGBA hex.
_HEX = re.compile(r"^(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass(frozen=True)
class _Field:
    """One image group written to the plate."""

    element: str
    #: ``(y, x)`` offset of this crop from the slide origin, in full-resolution slide pixels.
    offset_px: tuple[int, int]
    #: Microns per pixel, or ``None`` when the frame is in pixels.
    px_um: float | None
    nbytes: int


def _cell(row, column):
    """One column of a geometry row, or ``None``. Works for a Series, a Mapping or an object."""
    if row is None:
        return None
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            value = getter(column, None)
        except TypeError:          # an object whose `get` means something else
            value = getattr(row, column, None)
    else:
        value = getattr(row, column, None)
    return value


def grid_of(geometry_row, roi_id: str) -> tuple[int, int] | None:
    """``(rowIndex, columnIndex)`` for one core, or ``None``. Never invented.

    Reads the parquet's ``row``/``col`` first, then the ``R<r>C<c>`` identity.
    """
    r, c = _cell(geometry_row, "row"), _cell(geometry_row, "col")
    if r is not None and c is not None:
        try:
            return int(r), int(c)
        except (TypeError, ValueError):
            pass
    hit = _RC.match(str(roi_id))
    return (int(hit.group(1)), int(hit.group(2))) if hit else None


def _pixel_size(opened) -> float | None:
    """Microns per pixel, preferring the store's measured coord spacing over its metadata."""
    origin = getattr(opened, "origin", None)
    if origin is not None and getattr(origin, "px_um", None):
        return float(origin.px_um)
    px_um = getattr(getattr(opened, "meta", None), "px_um", None)
    return float(px_um) if px_um else None


def plate_pixel_size(targets, elements) -> tuple[float | None, str, list[str]]:
    """One microns-per-pixel for the whole plate, from the first store that records one.

    Returns ``(px_um, where it came from, stores that failed to open)``. ``None`` means pixels.
    """
    failed: list[str] = []
    for target in targets:
        for element in elements:
            art = target.arts.get(element.name)
            if art is None:
                continue
            try:
                px_um = _pixel_size(open_image(art, variable=element.variable))
            except Exception as exc:  # noqa: BLE001 - an unopenable store is the next one's turn
                failed.append(f"{target.where}/{element.name} ({type(exc).__name__}: {exc})")
                continue
            if px_um:
                return px_um, f"{target.where}/{element.name}", failed
    return None, "", failed


def transforms(offset_px, *, px_um: float | None, downsample: int = 1) -> list[dict]:
    """The ``coordinateTransformations`` for one ``(c, y, x)`` image: one scale, one translation.

    The scale includes ``downsample``; the translation does not. ``px_um`` of ``None`` writes the
    transforms in full-resolution slide pixels.
    """
    y0, x0 = offset_px
    step = float(px_um) if px_um else 1.0
    return [
        {"type": "scale", "scale": [1.0, step * downsample, step * downsample]},
        {"type": "translation", "translation": [0.0, y0 * step, x0 * step]},
    ]


def axes(px_um: float | None) -> list[dict]:
    """The ``c``, ``y``, ``x`` axes, in the order the spec requires."""
    unit = {"unit": "micrometer"} if px_um else {}
    return [{"name": "c", "type": "channel"},
            {"name": "y", "type": "space", **unit},
            {"name": "x", "type": "space", **unit}]


def _hex_color(value) -> str | None:
    """A colour as RGB hex for ``omero.channels[].color``, or ``None``. Accepts ``"R,G,B"``."""
    text = str(value).strip().lstrip("#")
    if _HEX.match(text):
        return text.upper()
    parts = text.split(",")
    if len(parts) == 3:
        try:
            rgb = [int(p) for p in parts]
        except ValueError:
            return None
        if all(0 <= v <= 255 for v in rgb):
            return "".join(f"{v:02X}" for v in rgb)
    return None


def _omero(meta, channels: int) -> dict:
    """Channel labels for the transitional ``omero`` key, so channels are not ``Channel:0..n``."""
    names = list(getattr(meta, "names", ()) or ())
    colors = list(getattr(meta, "colors", ()) or ())
    out = []
    for i in range(channels):
        entry = {"label": names[i] if i < len(names) else f"Channel:{i}", "active": True}
        color = _hex_color(colors[i]) if i < len(colors) and colors[i] else None
        if color:
            entry["color"] = color
        out.append(entry)
    return {"channels": out}


def _planes(opened, bounds, *, downsample: int, parent_shape=None):
    """Yield ``(plane, box)``: the padded crop one channel at a time.

    Do not wrap this in ``enumerate`` or hold the yielded value: either keeps the previous plane
    resident during the next read.
    """
    for view in channel_views(opened):
        arr, box = crop(view, bounds, parent_shape=parent_shape)
        # Stride decimation, never an area mean: these are quantitative intensities.
        arr = _to_writable(arr)
        if downsample > 1:
            arr = arr[:, ::downsample, ::downsample]
        yield arr, box
        del arr  # load-bearing: frees the plane before the next read


def _write_field(make_group, name, planes, *, channels, px_um, downsample, meta):
    """One core's one series as a single-resolution ``multiscales`` image group, plane by plane.

    ``make_group`` is called only after the first plane is read, so an unreadable store leaves no
    group. It writes one resolution level; add levels when a viewer needs them.
    """
    from zarr.codecs import ZstdCodec

    arr, box = next(planes)
    group = make_group()
    h, w = int(arr.shape[1]), int(arr.shape[2])
    chunks = (1, min(CHUNK, h), min(CHUNK, w))
    # A shard rounds up to whole chunks, so one channel is one file.
    shards = (SHARD_CHANNELS,
              -(-h // chunks[1]) * chunks[1],
              -(-w // chunks[2]) * chunks[2])
    array = group.create_array("0", shape=(channels or int(arr.shape[0]), h, w), dtype=arr.dtype,
                               chunks=chunks, shards=shards,
                               compressors=[ZstdCodec(level=ZSTD_LEVEL)])

    written, at = 0, 0
    while True:
        k = int(arr.shape[0])
        array[at:at + k] = arr
        at += k
        written += int(arr.nbytes)
        # Both `del`s are load-bearing: they free this plane before the next read.
        del arr
        nxt = next(planes, None)
        if nxt is None:
            break
        arr = nxt[0]
        del nxt

    group.attrs["ome"] = {
        "version": NGFF_VERSION,
        "multiscales": [{
            "name": name,
            "axes": axes(px_um),
            "datasets": [{"path": "0", "coordinateTransformations":
                          transforms((int(box[1]), int(box[0])), px_um=px_um,
                                     downsample=downsample)}],
        }],
        "omero": _omero(meta, int(array.shape[0])),
    }
    return _Field(name, (int(box[1]), int(box[0])), px_um, written)


def _plate(rows, columns, wells, *, field_count: int, name: str) -> dict:
    """The ``plate`` dictionary. Every row and column is declared, including empty ones.

    ``wells`` lists only wells that hold at least one image.
    """
    return {
        "name": name,
        "field_count": max(1, int(field_count)),
        "rows": [{"name": str(r)} for r in rows],
        "columns": [{"name": str(c)} for c in columns],
        "wells": [{"path": f"{r}/{c}", "rowIndex": ri, "columnIndex": ci}
                  for (r, c, ri, ci) in wells],
    }


def _drop(root, path: str) -> None:
    """Remove ``path`` from the hierarchy. A no-op where nothing exists."""
    try:
        del root[path]
    except (KeyError, OSError):
        pass


def _prune_empty(root, *paths: str) -> None:
    """Remove each path that exists and holds nothing. Innermost first."""
    for path in paths:
        try:
            group = root[path]
        except KeyError:
            continue
        if not (list(group.array_keys()) or list(group.group_keys())):
            _drop(root, path)


def _disk_bytes(path: pathlib.Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


#: Characters a 0.5 plate row or column name may not contain.
_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def row_label(route: tuple[str, ...]) -> str:
    """The row-name prefix for a core reached through ``route``, or ``"x"`` if nothing survives.

    A slide-anchored plate has rows such as ``TMA0R0``. ``route`` must be non-empty.
    """
    return _ALNUM.sub("", "".join(route)) or "x"


def _grid(targets, skipped, *, qualify: bool):
    """``(cells, rows, columns, bands)``: which well each core belongs in. Opens nothing.

    ``cells`` is ``{(rowName, columnName): target}``. ``rows`` and ``columns`` include empty ones.
    ``bands`` is ``[(label, [rowName])]``, one per route. Indices are unique per route.
    """
    per_route: dict[tuple, dict[tuple[int, int], object]] = {}
    labels, prefixes, used = {}, {}, set()
    for target in targets:
        cell = target.grid
        if cell is None:
            skipped.append(
                f"{target.where} (no row/col in the dearray parquet and the name is not R<r>C<c>, "
                f"so there is no well to put it in)")
            continue
        route = target.route if qualify else ()
        if route not in per_route:
            per_route[route] = {}
            # De-duplicate the label, not the prefix.
            base = row_label(route) if route else ""
            label, n = base, 2
            while route and label in used:
                label, n = f"{base}x{n}", n + 1
            used.add(label)
            labels[route] = label
            prefixes[route] = f"{label}R" if route else ""
        taken = per_route[route]
        if cell in taken:
            skipped.append(f"{target.where} (well {prefixes[route]}{cell[0]}/{cell[1]} is already "
                           f"taken by {taken[cell].where})")
            continue
        taken[cell] = target

    width = max((c for cells in per_route.values() for (_r, c) in cells), default=-1) + 1
    columns = [str(c) for c in range(width)]

    cells, rows, bands = {}, [], []
    for route in per_route:
        by_row: dict[int, list] = {}
        for (r, c), target in per_route[route].items():
            by_row.setdefault(r, []).append((c, target))
        band = []
        for r in range(max(by_row) + 1):
            name = f"{prefixes[route]}{r}"
            rows.append(name)
            band.append(name)
            for c, target in by_row.get(r, ()):
                cells[(name, str(c))] = target
        bands.append((labels[route], band))
    return cells, rows, columns, bands


def _write_plate(unit, *, recipe, placement, snapshot, downsample: int, qualify: bool) -> str:
    """Write this unit's cores as a plate. The body of both plate builders.

    Plate metadata is written last, from the wells that landed. A field path is the element's
    index in the recipe, so a well missing one element has fields ``0`` and ``2``.
    """
    import zarr

    targets, _boxes, skipped = _child_targets(unit, recipe, placement, snapshot)
    if not targets:
        raise ValueError(
            f"{unit.roi_name}: no core resolved to a readable image, so there is no plate to "
            f"write" + (f" ({'; '.join(skipped[:5])})" if skipped else ""))

    cells, rows, columns, bands = _grid(targets, skipped, qualify=qualify)
    if not cells:
        raise ValueError(
            f"{unit.roi_name}: no core carries a grid position, so a plate cannot say where any "
            f"of them belong" + (f" ({'; '.join(skipped[:5])})" if skipped else ""))

    elements = [e for e in recipe.elements if e.kind == "image"]
    if not elements:
        raise ValueError(f"{unit.roi_name}: the recipe declares no image element to write")

    out = pathlib.Path(unit.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(out, mode="w", zarr_format=3)

    index_of = ({name: i for i, name in enumerate(rows)},
                {name: i for i, name in enumerate(columns)})
    ordered = sorted(cells, key=lambda k: (index_of[0][k[0]], index_of[1][k[1]]))

    px_um, px_from, px_failed = plate_pixel_size([cells[k] for k in ordered], elements)
    fields, filled, disagree, measured_late = [], [], [], []
    for (r, c) in ordered:
        target = cells[(r, c)]
        images = []
        # A well group exists once a plane lands; `_prune_empty` removes one a failed field left.
        def _make(index, r=r, c=c):
            return lambda: root.require_group(r).require_group(c).require_group(str(index))

        for index, element in enumerate(elements):
            art = target.arts.get(element.name)
            if art is None:
                continue
            try:
                opened = open_image(art, variable=element.variable)
                planes = _planes(opened, target.bounds, downsample=downsample,
                                 parent_shape=target.parent)
                field = _write_field(_make(index), element.name, planes,
                                     channels=channels_of(opened)[0], px_um=px_um,
                                     downsample=downsample, meta=opened.meta)
            except Exception as exc:  # noqa: BLE001 - one unreadable core must not lose the plate
                skipped.append(f"{target.where}/{element.name} ({type(exc).__name__}: {exc})")
                # A field is all or nothing: drop a partly written one.
                _drop(root, f"{r}/{c}/{index}")
                continue
            own = _pixel_size(opened)
            if own and px_um and not math.isclose(own, px_um, rel_tol=1e-3):
                disagree.append(f"{target.where}/{element.name} measures {own} um")
            elif own and not px_um:
                measured_late.append(f"{target.where}/{element.name} measures {own} um")
            images.append({"path": str(index)})
            fields.append(field)
            # Load-bearing: frees the last plane before the next core is read.
            del opened, planes, field
        if not images:
            _prune_empty(root, f"{r}/{c}", r)
            skipped.append(f"{target.where} (no element could be read, so well {r}/{c} is not "
                           f"in the plate)")
            continue
        filled.append(((r, c), len(images)))
        root[r][c].attrs["ome"] = {"version": NGFF_VERSION, "well": {"images": images}}

    if not fields:
        raise ValueError(
            f"{unit.roi_name}: every core failed to read, so the plate holds no pixels"
            + (f" ({'; '.join(skipped[:5])})" if skipped else ""))

    root.attrs["ome"] = {
        "version": NGFF_VERSION,
        "plate": _plate(rows, columns,
                        [(r, c, index_of[0][r], index_of[1][c]) for ((r, c), _n) in filled],
                        field_count=max(n for _cell, n in filled),
                        name=str(unit.roi_name)),
    }

    written = sum(f.nbytes for f in fields)
    on_disk = _disk_bytes(out)
    detail = (f"{len(filled)} cores in a {len(rows)}x{len(columns)} plate; "
              f"{len(fields)} image groups ({', '.join(sorted({f.element for f in fields}))}); "
              f"{_human(written)} of pixels -> {_human(on_disk)} on disk "
              f"(zstd-{ZSTD_LEVEL}, {written / max(1, on_disk):.1f}x)"
              + (f", SAMPLED {downsample}x by stride decimation" if downsample > 1 else ""))
    if qualify:
        detail += ("; " + ", ".join(f"{label or 'ungrouped'} rows {band[0]}-{band[-1]}"
                                    for label, band in bands))
    detail += (f"; {px_um} um/px from {px_from}" if px_um else
               "; NO store the sample could read recorded a pixel size, so the plate is in "
               "full-resolution slide PIXELS and its axes carry no unit")
    if px_failed:
        detail += (f"; PIXEL SIZE UNREAD - {len(px_failed)} store(s) would not open while "
                   f"sampling it ({'; '.join(px_failed[:3])})" if px_um is None else
                   f"; {len(px_failed)} store(s) were skipped while sampling it "
                   f"({'; '.join(px_failed[:3])})")
    if measured_late:
        detail += (f"; PIXEL SIZE FOUND TOO LATE - {len(measured_late)} field(s) did measure one "
                   f"when they were written, after the plate's frame was already settled in "
                   f"pixels ({'; '.join(measured_late[:3])})")
    if disagree:
        detail += (f"; PIXEL SIZE DISAGREEMENT - {len(disagree)} field(s) measure something else "
                   f"and were written in the plate's frame anyway ({', '.join(disagree[:3])})")
    return detail + (f"; SKIPPED {', '.join(skipped[:5])}" if skipped else "")


def build_tma_plate(unit, *, recipe, placement, snapshot, downsample: int = 1, **_kw) -> str:
    """Write this TMA as an OME-NGFF plate, one well per core and one field per series."""
    return _write_plate(unit, recipe=recipe, placement=placement, snapshot=snapshot,
                        downsample=downsample, qualify=False)


def build_slide_plate(unit, *, recipe, placement, snapshot, downsample: int = 1, **_kw) -> str:
    """Every core of every TMA on this slide as one plate. Rows are qualified by TMA.

    Core geometry is already in slide pixels, so no extra transform is applied.
    """
    return _write_plate(unit, recipe=recipe, placement=placement, snapshot=snapshot,
                        downsample=downsample, qualify=True)
