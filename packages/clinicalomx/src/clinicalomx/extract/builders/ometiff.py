"""OME-TIFF builders at core, TMA and slide level, one series per available element.

Every series of one file is cropped with the same padded box, so the series are pixel-registered.
Channel names come from the bgnormed zarr's coords, else from the QptiffChannels Records.
``build_tma_mosaic`` writes a sparse, pyramidal mosaic of every core under a TMA or slide.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from clinicalomx.artifacts import newest_artifact
from clinicalomx.extract.builders import geo
from clinicalomx.extract.builders.imaging import (
    DECLARED,
    ChannelMeta,
    channel_view,
    channels_of,
    crop,
    crop_bounds,
    open_image,
)
from clinicalomx.extract.builders.mosaic import (
    TILE,
    EmptyMosaic,
    MosaicError,
    Placed,
    _human,
    estimate_bytes,
)
from clinicalomx.extract.builders.spatialdata import _ChildUnit
from clinicalomx.extract.coords import crop_box
from clinicalomx.extract.keys import path_from
from clinicalomx.names import FEATURES
from clinicalomx.snapshot import INHERITED_FEATURES, type_name_of

#: Written to the OME model when a series' array is integer vs floating point.
_PIXEL_TYPE = {"uint8": "uint8", "uint16": "uint16", "uint32": "uint32",
               "int16": "int16", "int32": "int32",
               "float32": "float", "float64": "double"}


def _resolve(unit, snapshot):
    """``(roi record, values, geometry row, frame)`` - everything placement needs, read once."""
    import lamindb as ln


    roi = ln.Record.filter(uid=unit.roi_uid).one_or_none()
    if roi is None:
        raise LookupError(f"{unit.roi_name}: the ROI Record has gone since the plan was made")
    values = snapshot.values(roi)

    parquet = newest_artifact(values.get(FEATURES.SOURCE_ARTIFACT)
                              or values.get(FEATURES.ROI_PARQUET))
    if parquet is None:
        return roi, values, None, None
    frame = geo.read_frame(parquet.path)   # raises FrameUnknown rather than assume
    row = geo.row_for(geo.read_geometry(parquet.path), roi_name=str(roi.name),
                      roi_id=values.get(FEATURES.ROI_ID), parquet_row=values.get("parquet_row"))
    return roi, values, row, frame


def _artifact_for(roi, role, values, snapshot):
    """The newest Artifact under ``role`` for this ROI, inheriting the slide's scan where needed."""
    return newest_artifact(
        snapshot.artifact_keys(roi, role, inherit=role in INHERITED_FEATURES))


def channel_names(art, snapshot) -> list[str] | None:
    """Channel names from the instance's Records, for a source with no channel coords."""
    try:
        values = art.features.get_values()
    except Exception:  # noqa: BLE001 - an artifact with no features is normal, not an error
        values = {}
    joined = values.get("qptiff_channel_names")
    if isinstance(joined, str) and joined.strip():
        return [c.strip() for c in joined.split(",") if c.strip()]

    channels = values.get("qptiff_channels")
    if not channels:
        return None
    many = channels if isinstance(channels, (list, tuple, set)) else [channels]
    try:
        snapshot.load_values("QptiffChannels", features=["qptiff_ch_index", "qptiff_ch_name"])
    except Exception:  # noqa: BLE001 - the type may not exist on every instance
        return None
    rows = []
    for value in many:
        got = snapshot._values.get(getattr(value, "uid", None), {})
        if got.get("qptiff_ch_name"):
            rows.append((got.get("qptiff_ch_index", 0), str(got["qptiff_ch_name"])))
    return [name for _i, name in sorted(rows)] or None


def to_ome(series, meta: ChannelMeta):
    """An ``ome_types.OME``: one Image per series, N named channels each."""
    from ome_types import OME
    from ome_types.model import Channel, Image, Pixels, TiffData

    n_c = max(meta.n, series[0][1].shape[0])
    size_y, size_x = int(series[0][1].shape[1]), int(series[0][1].shape[2])

    def channels():
        out = []
        for i in range(n_c):
            name = meta.names[i] if i < len(meta.names) else f"Channel:{i}"
            colour = meta.colors[i] if i < len(meta.colors) else None
            emis = meta.emission_nm[i] if i < len(meta.emission_nm) else None
            fluor = meta.fluorophore[i] if i < len(meta.fluorophore) else None
            kwargs = {"id": f"Channel:{i}", "name": name, "samples_per_pixel": 1}
            if colour:
                from ome_types.model.simple_types import Color

                try:
                    kwargs["color"] = Color(colour)
                except (ValueError, TypeError):
                    pass  # an unparseable colour is cosmetic
            if emis and emis > 0:
                kwargs["emission_wavelength"] = emis
            if fluor and fluor not in ("", "nan", "None"):
                kwargs["fluor"] = fluor
            out.append(Channel(**kwargs))
        return out

    images = []
    for si, (name, arr, _dtype) in enumerate(series):
        px_kwargs = {}
        if meta.px_um:
            px_kwargs = {"physical_size_x": meta.px_um, "physical_size_y": meta.px_um,
                         "physical_size_x_unit": "µm", "physical_size_y_unit": "µm"}
        images.append(Image(id=f"Image:{si}", name=name, pixels=Pixels(
            id=f"Pixels:{si}", dimension_order="XYCZT",
            type=_PIXEL_TYPE.get(str(arr.dtype), "float"),
            size_c=int(arr.shape[0]), size_t=1, size_z=1, size_x=size_x, size_y=size_y,
            channels=channels(),
            tiff_data_blocks=[TiffData(ifd=si * n_c, plane_count=n_c,
                                       first_c=0, first_z=0, first_t=0)],
            **px_kwargs)))
    return OME(images=images)


#: tifffile encoder buffer in bytes. This sets peak memory for a tile producer.
_WRITE_BUFFER = 32 * 1024**2


def write_ome_tiff(series, ome, out_path, *, compression="zlib", pyramid=None) -> None:
    """One multi-series, tiled, zlib-compressed BigTIFF with ``ome`` as its OME-XML.

    A series is an array or a :class:`~clinicalomx.extract.builders.mosaic.Sparse`; a ``None``
    tile is written absent and reads as zero. ``pyramid(si)`` returns series ``si``'s overview
    levels, written as SubIFDs of the base and released after that series is written.
    """
    import tifffile
    from ome_types import to_xml

    ome_xml = to_xml(ome).encode("utf-8")   # UTF-8: the µm sign is in there
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(out, bigtiff=True, ome=False) as tif:
        for si, (_name, arr, _dtype) in enumerate(series):
            common = {"photometric": "minisblack", "metadata": False, "contiguous": False,
                      "compression": compression}
            over = tuple(pyramid(si)) if pyramid is not None else ()
            _write_level(tif, arr, description=ome_xml if si == 0 else None,
                         subifds=len(over) or None, **common)
            for one in over:
                _write_level(tif, one, description=None, subfiletype=1, **common)
            # Release after the last level: deeper levels read the first level's cache.
            for one in over:
                one.release()


def _write_level(tif, arr, **kw) -> None:
    """One IFD, whether ``arr`` produces tiles or is an ordinary array."""
    if hasattr(arr, "tiles"):
        tif.write(arr.tiles(), shape=arr.shape, dtype=arr.dtype, tile=tuple(arr.tile),
                  buffersize=_WRITE_BUFFER, **kw)
    else:
        tif.write(arr, tile=TILE, **kw)


def _to_writable(arr):
    """Cast float64 to float32, with NaN as zero. Other dtypes pass through."""
    import numpy as np

    return np.nan_to_num(arr).astype(np.float32) if str(arr.dtype) == "float64" else arr


def _written_dtype(dtype) -> str:
    """The dtype the writer stores for a source ``dtype``."""
    import numpy as np

    return str(_to_writable(np.empty(0, dtype=dtype)).dtype)


def build_core(unit, *, recipe, placement, snapshot, **_kw) -> str:
    """Write this Core as an OME-TIFF, one series per element the plan resolved."""
    roi, values, row, frame = _resolve(unit, snapshot)
    bounds = placement.bounds(values=values, geometry_row=row,
                              container_attrs={"frame": frame})
    scan = _scan_shape(roi, snapshot)

    series, meta, skipped = [], ChannelMeta(), []
    for element in recipe.elements:
        role = unit.elements.get(element.name)
        if role is None:
            continue
        art = _artifact_for(roi, role, values, snapshot)
        if art is None:
            skipped.append(f"{element.name} ({role}: no readable Artifact)")
            continue
        opened = open_image(art, variable=element.variable)
        arr, _box = crop(opened, bounds, parent_shape=scan)
        arr = _to_writable(arr)
        series.append((element.name, arr, str(arr.dtype)))
        if opened.meta.n and not meta.n:
            meta = opened.meta

    if not series:
        raise ValueError(
            f"{unit.roi_name}: no element resolved to a readable image"
            + (f" ({'; '.join(skipped)})" if skipped else ""))

    if not meta.n:
        art = _artifact_for(roi, next(iter(unit.elements.values())), values, snapshot)
        names = channel_names(art, snapshot) if art is not None else None
        meta = ChannelMeta(names=tuple(names or ()), source="records")

    shapes = {s[1].shape[1:] for s in series}
    if len(shapes) > 1:
        raise ValueError(
            f"{unit.roi_name}: the series are not pixel-registered - shapes {sorted(shapes)}. "
            f"They are meant to be the same field of view, so one of them was cropped with a "
            f"different box.")

    write_ome_tiff(series, to_ome(series, meta), unit.output)
    named = "named" if meta.n else "UNNAMED - no channel record on the source"
    return (f"{len(series)} series ({', '.join(n for n, _, _ in series)}); "
            f"{series[0][1].shape[0]}c x {series[0][1].shape[-1]}x{series[0][1].shape[-2]}px; "
            f"channels {named}"
            + (f"; SKIPPED {', '.join(skipped)}" if skipped else ""))


#: A TMA's own field is cropped from the slide exactly as a core's is.
build_tma = build_core


@dataclass(frozen=True)
class _Target:
    """One core of a mosaic: where it goes, and which Artifact fills each series. No pixels."""

    label: str
    bounds: tuple
    #: ``{element name: Artifact}``. An element absent here has nothing to read on this core.
    arts: dict
    #: ``(rowIndex, columnIndex)`` on the TMA grid, or ``None``. Needed by the NGFF plate builder.
    grid: tuple[int, int] | None = None
    #: Record-name segments between the anchor and this target, both exclusive: ``()`` under its
    #: own TMA, ``("TMA0",)`` under the slide.
    route: tuple[str, ...] = ()
    #: ``(h, w)`` of the scan this target's bounds are measured in, or ``None``.
    parent: tuple[int, int] | None = None

    @property
    def where(self) -> str:
        """The name a report uses: ``R0C0`` under its TMA, ``TMA1/R0C0`` under a slide."""
        return "/".join((*self.route, self.label))


def _scan_shape(roi, snapshot) -> tuple[int, int] | None:
    """``(h, w)`` of the scan an ROI's geometry is measured in, or ``None``. Never raises."""
    try:
        art = _artifact_for(roi, FEATURES.SOURCE_IMAGE, None, snapshot)
        if art is None:
            return None
        h, w = open_image(art).shape_yx
    except Exception:  # noqa: BLE001 - a scan we cannot measure is not a build we cannot do
        return None
    return (int(h), int(w)) if h and w else None


def _child_targets(unit, recipe, placement, snapshot):
    """``([_Target], padded boxes, skipped)``: where every core goes. Opens no store."""
    from clinicalomx.extract.builders.ngff import grid_of

    parent = snapshot.record_by_uid(unit.roi_uid)
    if parent is None:
        raise LookupError(f"{unit.roi_name}: the ROI Record has gone since the plan was made")

    scan = _scan_shape(parent, snapshot)
    targets, boxes, skipped = [], [], []
    for child in recipe.targets_under(parent, snapshot):
        name, roi_type = str(child.name), type_name_of(child)
        placer = placement.for_roi(roi_type) if hasattr(placement, "for_roi") else placement
        if placer is None:
            skipped.append(f"{name} ({roi_type}: no placement declared for this subtype)")
            continue
        stub = _ChildUnit(child.uid, name, unit.child_elements.get(name, unit.elements))
        try:
            roi, values, row, frame = _resolve(stub, snapshot)
            bounds = placer.bounds(values=values, geometry_row=row,
                                   container_attrs={"frame": frame})
        except Exception as exc:  # noqa: BLE001 - one unplaceable core must not lose the TMA
            skipped.append(f"{name} ({type(exc).__name__}: {exc})")
            continue
        if bounds is None:
            skipped.append(f"{name} (nothing on this core says where it belongs)")
            continue

        arts = {}
        for element in recipe.elements:
            if element.kind != "image":
                continue
            role = stub.elements.get(element.name)
            if role is None:
                continue
            art = _artifact_for(roi, role, values, snapshot)
            if art is not None:
                arts[element.name] = art
        if not arts:
            skipped.append(f"{name} (no element resolved to a readable Artifact)")
            continue

        label = str(values.get(FEATURES.ROI_ID) or name.rsplit(":", 1)[-1])
        targets.append(_Target(label, tuple(bounds), arts, grid_of(row, label),
                               path_from(unit.roi_name, name), scan))
        boxes.append(crop_box(bounds))

    return targets, boxes, skipped


@dataclass(frozen=True)
class _Plane:
    """Read one channel of one core, cropped to the box that core was placed at."""

    opened: object
    bounds: tuple
    #: The scan extent the core was placed against.
    parent: tuple[int, int] | None = None

    def __call__(self, channel: int):
        view = channel_view(self.opened, channel)
        arr, _box = crop(view, self.bounds, parent_shape=self.parent)
        # `channel_view` returns the whole store when the axes do not separate.
        return _to_writable(arr)[0 if view is not self.opened else channel]


@dataclass(frozen=True)
class _Series:
    """One element resolved over every core: what to write, and where each core lands."""

    name: str
    channels: int
    dtype: str
    meta: ChannelMeta
    #: :class:`mosaic.Placed`, in overlap-priority order.
    sources: tuple
    #: The clamped boxes.
    boxes: tuple
    #: Cores with no Artifact for this element.
    absent: tuple = ()
    #: Cores with an Artifact for this element that could not be placed. Always a defect.
    dropped: tuple = ()
    #: Cores placed from the box that produced them, not from a recorded origin.
    declared: tuple = ()


def _place(targets, element, skipped) -> _Series:
    """Every core's box and reader for one series, from metadata alone. Reads no pixel.

    Raises :class:`MosaicError` when cores disagree on channel count or written dtype.
    """
    sources, boxes, absent, dropped, declared = [], [], [], [], []
    channels, dtype, meta = 0, "", ChannelMeta()
    why = "no core carries this element"
    for target in targets:
        art = target.arts.get(element.name)
        if art is None:
            absent.append(target.where)
            continue
        try:
            opened = open_image(art, variable=element.variable)
            found, bad = channels_of(opened)
            if bad:
                raise ValueError(bad)
            box, _ox, _oy, how = crop_bounds(opened, target.bounds, parent_shape=target.parent)
        except Exception as exc:  # noqa: BLE001 - one unplaceable core must not lose the TMA
            why = f"{target.where}: {type(exc).__name__}: {exc}"
            skipped.append(f"{target.where}/{element.name} ({type(exc).__name__}: {exc})")
            dropped.append(f"{target.where} ({type(exc).__name__}: {exc})")
            continue
        written = _written_dtype(opened.array.dtype)
        if not channels:
            channels, dtype, meta = found, written, opened.meta
        elif found != channels:
            raise MosaicError(
                f"{target.where} carries {found} channels where this mosaic was declared with "
                f"{channels}. Compositing it would silently drop bands from whichever has more.")
        elif written != dtype:
            raise MosaicError(
                f"{target.where} is dtype {written} where this mosaic was declared {dtype}. A "
                f"mosaic is one array, and there is no correct answer for which type it is.")
        if how == DECLARED:
            declared.append(target.where)
        sources.append(Placed(target.where, box, _Plane(opened, target.bounds, target.parent)))
        boxes.append(box)

    if not sources:
        raise EmptyMosaic(why)
    return _Series(element.name, channels, dtype, meta, tuple(sources), tuple(boxes),
                   tuple(absent), tuple(dropped), tuple(declared))


def build_tma_mosaic(unit, *, recipe, placement, snapshot, downsample: int = 1, **_kw) -> str:
    """Every core of this TMA in one OME-TIFF, each series a sparse, pyramidal mosaic.

    All series share one canvas. Placement and the size ceiling are checked before any pixel is
    read, and a tile no core covers is never written.
    """
    from clinicalomx.extract.builders.mosaic import (
        EmptyMosaic,
        MosaicError,
        Sparse,
        cache_factor,
        canvas_shape,
        check_footprint,
        overview_sources,
        pyramid_levels,
        union_bounds,
    )

    targets, boxes, skipped = _child_targets(unit, recipe, placement, snapshot)
    if not targets:
        raise ValueError(
            f"{unit.roi_name}: no core resolved to a readable image, so there is nothing to "
            f"composite" + (f" ({'; '.join(skipped[:5])})" if skipped else ""))
    # The predicted padded boxes, as `layout._mosaic_layout` unions them.
    bounds = union_bounds(boxes)

    resolved, missing, meta = [], [], ChannelMeta()
    for element in recipe.elements:
        if element.kind != "image":
            continue
        try:
            got = _place(targets, element, skipped)
        except EmptyMosaic as exc:
            missing.append(f"{element.name} ({exc})")
            continue
        except MosaicError as exc:
            raise ValueError(f"{unit.roi_name}/{element.name}: {exc}") from exc
        resolved.append(got)
        if got.meta.n and not meta.n:
            meta = got.meta
    if not resolved:
        raise ValueError(
            f"{unit.roi_name}: no element could be read on any core, so there is nothing to "
            f"composite" + (f" ({'; '.join(skipped[:5])})" if skipped else ""))
    _refuse_a_hollow_series(unit, resolved, missing)

    # Checked before any pixel, over what every level will store.
    factors = {}
    for got in resolved:
        factors[got.name] = pyramid_levels(bounds, downsample=downsample)
        try:
            check_footprint(got.boxes, got.dtype, channels=got.channels,
                            downsamples=factors[got.name], bounds=bounds)
        except MosaicError as exc:
            raise ValueError(f"{unit.roi_name}/{got.name}: {exc}") from exc

    series, sparse = [], []
    for got in resolved:
        one = Sparse(canvas_shape(bounds, channels=got.channels, downsample=downsample),
                     got.dtype, int(bounds[0]), int(bounds[1]),
                     sources=got.sources, downsample=downsample)
        sparse.append(one)
        series.append((got.name, one, got.dtype))

    ome = to_ome(series, meta)
    # Each series' description comes from its own overview chain and core count.
    for image, got, one in zip(ome.images, resolved, sparse, strict=True):
        own = list(factors[got.name][1:])
        image.description = (f"sparse mosaic of {len(one.sources)} PLACED cores, which is what "
                             f"is known before the pixels are read; "
                             f"origin_x={one.origin_x} origin_y={one.origin_y} "
                             f"downsample={one.downsample} (full-resolution slide pixels); "
                             f"tiles outside a core are absent and read as zero"
                             + (f"; SubIFD overview levels at {own}"
                                if own else "; one resolution level"))

    # One extra pixel pass per series builds every overview level, made on demand.
    over = []

    def make_levels(si):
        got = resolved[si]
        steps = factors[got.name]
        if len(steps) < 2:
            return ()
        cache = cache_factor(steps, got.boxes, got.dtype, channels=got.channels)
        made = tuple(
            Sparse(canvas_shape(bounds, channels=got.channels, downsample=step), got.dtype,
                   int(bounds[0]), int(bounds[1]),
                   sources=sources, downsample=step, decimate=decimate)
            for step, (sources, decimate) in zip(
                steps[1:], overview_sources(got.sources, steps, cache), strict=True))
        over.append((got.name, made))
        return made

    try:
        write_ome_tiff(series, ome, unit.output, pyramid=make_levels)
    except MosaicError as exc:
        # A partly written file opens normally, so delete it.
        pathlib.Path(unit.output).unlink(missing_ok=True)
        raise ValueError(f"{unit.roi_name}: {exc}") from exc

    lost = []
    for got, one in zip(resolved, sparse, strict=True):
        lost.extend(f"{got.name}/{s}" for s in one.skipped)
    if lost:
        # A placed core that could not be read: the series is short a core, so delete the file.
        pathlib.Path(unit.output).unlink(missing_ok=True)
        raise ValueError(
            f"{unit.roi_name}: {len(lost)} core/series pair(s) were placed and then could not be "
            f"read, so those series are short a core the others carry: {'; '.join(lost)}. The "
            f"file is removed rather than left holding a gap nothing in it records.")
    # An overview drop is reported, not fatal: the core is present at the base.
    gaps = []
    for name, made in over:
        seen = set()
        for level in made:
            for why in level.skipped:
                core = _core_of(why)
                if core in seen:
                    continue
                seen.add(core)
                skipped.append(f"{name}/OVERVIEW {why}")
                gaps.append(f"{name}/{core}")
    overlaps = sorted({o[0] for one in sparse for o in one.overlaps})
    written = sum(one.stored_tiles for one in sparse)
    empty = sum(one.empty_tiles for one in sparse)
    stored = sum(one.stored_bytes for one in sparse)
    dense = sum(_dense_bytes(one) for one in sparse)
    extra = sum(level.stored_bytes for _name, made in over for level in made)
    first = sparse[0]
    placed = "; ".join(f"{got.name} {len(one.placed)} of {len(one.sources)} at {one.shape[0]}c"
                       for got, one in zip(resolved, sparse, strict=True))
    detail = (f"{len(series)} series ({', '.join(n for n, _, _ in series)}); "
              f"placed cores per series: {placed} - written into "
              f"{first.shape[2]}x{first.shape[1]}px "
              f"at origin ({first.origin_x}, {first.origin_y})"
              + (f", downsampled {downsample}x" if downsample > 1 else "")
              + f"; {written} tiles written and {empty} left ABSENT, so {_human(stored)} of "
                f"pixels stored against {_human(dense)} of canvas before compression"
              + (f"; PYRAMID {_levels_phrase(factors)}, adding {_human(extra)} "
                 f"({100 * extra / stored:.0f}% of the base) so a viewer can open the file"
                 if over and stored else
                 "; NO pyramid - the base already fits a viewer's tile cache"))
    if gaps:
        detail += (f"; PYRAMID GAP {', '.join(gaps)} - read at the base and not at the overview "
                   f"levels, so it vanishes when a viewer zooms out")
    if missing:
        detail += (f"; MISSING series {', '.join(missing)} - the recipe resolved them and no core "
                   f"produced one")
    if overlaps:
        detail += f"; OVERLAP {', '.join(overlaps[:5])} - padded boxes touched, first write kept"
    frames = [f"{got.name} {len(got.declared)}" for got in resolved if got.declared]
    if frames:
        detail += (f"; FRAME DECLARED for {', '.join(frames)} core(s) - those stores record no "
                   f"origin of their own and were placed at the padded box that produced them")
    thin = [f"{got.name} without {', '.join(got.absent)}"
            for got in resolved if got.absent]
    if thin:
        detail += (f"; NOT EVERY SERIES HOLDS EVERY CORE: {'; '.join(thin)} - those cores carry "
                   f"no artifact under that element, so nothing was dropped")
    return detail + (f"; SKIPPED {', '.join(skipped[:5])}" if skipped else "")


def _refuse_a_hollow_series(unit, resolved, missing) -> None:
    """Raise when a series would hold fewer cores than its siblings because placement failed.

    A core with no Artifact for an element is not a failure; it is reported in the detail.
    """
    hollow = [(got.name, got.dropped) for got in resolved if got.dropped]
    if not hollow:
        return
    counts = ", ".join(f"{got.name} {len(got.sources)}" for got in resolved)
    detail = "; ".join(f"{name}: {', '.join(why)}" for name, why in hollow)
    raise ValueError(
        f"{unit.roi_name}: {sum(len(w) for _n, w in hollow)} core(s) carry an artifact for a "
        f"series and could not be placed into it, so that series would hold fewer cores than its "
        f"siblings ({counts}) while looking exactly like a complete one. Every reason, none "
        f"truncated - {detail}"
        + (f". Series no core could supply at all: {', '.join(missing)}" if missing else "")
        + ". A merged OME-TIFF's series are the same cores or it is not a merged OME-TIFF.")


def _core_of(why: str) -> str:
    """The core name from a ``Sparse.skipped`` entry, ``<name> at channel <c> (...)``."""
    return why.split(" at channel", 1)[0]


def _levels_phrase(factors) -> str:
    """Which overview levels each series carries, said once where they agree."""
    steps = {name: list(f[1:]) for name, f in factors.items()}
    same = {tuple(s) for s in steps.values()}
    if len(same) == 1:
        one = next(iter(same))
        return f"{len(one)} overview level(s) at {list(one)} on every series"
    return "overview levels " + ", ".join(f"{name} at {s or 'none'}" for name, s in steps.items())


def _dense_bytes(one) -> int:
    """Dense byte size of the same mosaic."""
    return estimate_bytes(one.shape, one.dtype)
