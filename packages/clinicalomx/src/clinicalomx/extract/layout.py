"""A preview of the object a plan will produce: series, order, channels, size, dtype and source.

Decides nothing and reads no pixel. Every number comes from the builder function that produces it
at build time. Dimensions are measured on one representative unit; absences are counted over every
unit and child.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from clinicalomx.extract.assemble import AssemblyError, _recipe, resolve_dotted
from clinicalomx.extract.builders import geo
from clinicalomx.extract.builders.imaging import ChannelMeta, channels_of
from clinicalomx.extract.builders.ometiff import _levels_phrase
from clinicalomx.extract.builders.ometiff import _written_dtype as written
from clinicalomx.extract.builders.spatialdata import _placement_order
from clinicalomx.extract.coords import PlacementError, crop_box
from clinicalomx.extract.keys import key_for
from clinicalomx.names import FEATURES
from clinicalomx.snapshot import INHERITED_FEATURES, Snapshot, type_name_of

#: How many element keys the text tree lists per slot. ``to_dict`` always has them all.
MAX_TREE_KEYS = 12


@dataclass(frozen=True)
class Piece:
    """One addressable thing in the finished object, and what will fill it.

    ``key`` is its name in the output; ``element`` is the recipe slot that put it there.
    """

    key: str
    #: ``series`` for an OME-TIFF, or the SpatialData slot (``images``/``labels``/...).
    slot: str
    element: str
    index: int = 0
    role: str | None = None
    variable: str | None = None
    #: The artifact key this is read from. Two pieces sharing one are two arrays in one store.
    source: str | None = None
    dims: tuple[str, ...] = ()
    shape: tuple[int, ...] = ()
    dtype: str = ""
    channels: tuple[str, ...] = ()
    px_um: float | None = None
    #: SpatialData only: the coordinate system, and the transformation into it.
    space: str = ""
    transform: str = ""
    #: A table's annotation target and join column. Both ``None`` on a floating table.
    annotates: str | None = None
    instance_key: str | None = None
    #: Uncompressed bytes, from the shape and the dtype.
    size_bytes: int = 0
    #: Mosaic only: uncompressed bytes the file holds across every pyramid level. Zero otherwise.
    stored_bytes: int = 0
    #: How many ROIs contribute to this piece. Above 1 only for a mosaic.
    n_from: int = 1
    #: Why the measurement is absent, or what is odd about it.
    note: str = ""

    @property
    def measured(self) -> bool:
        return bool(self.shape)

    def to_dict(self) -> dict:
        return {"key": self.key, "slot": self.slot, "element": self.element, "index": self.index,
                "role": self.role, "variable": self.variable, "source": self.source,
                "dims": list(self.dims), "shape": list(self.shape), "dtype": self.dtype,
                "channels": list(self.channels), "px_um": self.px_um,
                "space": self.space, "transform": self.transform,
                "annotates": self.annotates, "instance_key": self.instance_key,
                "size_bytes": self.size_bytes, "stored_bytes": self.stored_bytes,
                "n_from": self.n_from, "note": self.note}


@dataclass(frozen=True)
class Absence:
    """Something the output will not have, stated as an effect on the output.

    ``state`` is the resolver's word; ``effect`` is the sentence about the file.
    """

    element: str
    effect: str
    state: str = ""
    #: ``(contributing, total)`` for a partial absence.
    partial: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        return {"element": self.element, "effect": self.effect, "state": self.state,
                "partial": list(self.partial) if self.partial else None}


@dataclass(frozen=True)
class TargetLayout:
    """What comes out, and where each chosen element lands in it."""

    target: str
    layout: str
    recipe: str
    n_outputs: int
    #: The representative output's path. A ``per_child`` plan writes ``n_outputs`` of these.
    output: str = ""
    #: The ROI the dimensions were read from. Empty where nothing was measured.
    measured_on: str = ""
    pieces: tuple[Piece, ...] = ()
    absences: tuple[Absence, ...] = ()
    #: Metadata reads this cost: artifact-row lookups plus store opens.
    reads: int = 0
    notes: tuple[str, ...] = ()
    #: The OME-XML this file will carry, from the builder's ``to_ome``. Empty for other targets.
    ome_xml: str = ""

    @property
    def measured(self) -> bool:
        return any(p.measured for p in self.pieces)

    def to_dict(self) -> dict:
        return {"target": self.target, "layout": self.layout, "recipe": self.recipe,
                "n_outputs": self.n_outputs, "output": self.output,
                "measured_on": self.measured_on,
                "pieces": [p.to_dict() for p in self.pieces],
                "absences": [a.to_dict() for a in self.absences],
                "reads": self.reads, "notes": list(self.notes), "ome_xml": self.ome_xml}

    def tree(self, *, max_keys: int = MAX_TREE_KEYS) -> str:
        """The layout as a text tree, with keys truncated at ``max_keys``."""
        head = f"{self.target}  [{self.layout}]"
        if self.n_outputs != 1:
            head += f"  x{self.n_outputs}"
        lines = [head, f"  {self.output}" if self.output else ""]
        by_slot: dict[str, list[Piece]] = {}
        for piece in self.pieces:
            by_slot.setdefault(piece.slot, []).append(piece)
        for slot, pieces in by_slot.items():
            lines.append(f"  {slot} ({len(pieces)})")
            for piece in pieces[:max_keys]:
                lines.append("    " + _piece_line(piece))
                for detail in _piece_details(piece):
                    lines.append("        " + detail)
            if len(pieces) > max_keys:
                lines.append(f"    ... and {len(pieces) - max_keys} more")
        for absence in self.absences:
            lines.append(f"  ABSENT  {absence.element}: {absence.effect}")
        for note in self.notes:
            lines.append(f"  note    {note}")
        return "\n".join(line for line in lines if line != "")


def _piece_line(piece: Piece) -> str:
    shape = " x ".join(str(n) for n in piece.shape)
    size = f"{shape} {piece.dtype}" if piece.shape else "dimensions not read"
    if piece.size_bytes:
        size += f"  {_human(piece.size_bytes)}"
    if piece.stored_bytes:
        size += f" of canvas, {_human(piece.stored_bytes)} stored"
    fan = f"  (from {piece.n_from} ROIs)" if piece.n_from > 1 else ""
    return f"{piece.key}   {size}{fan}"


def _piece_details(piece: Piece) -> list[str]:
    out = [f"from  {piece.element} <- {piece.role or '-'}"
           + (f"#{piece.variable}" if piece.variable else "")
           + (f"   {piece.source}" if piece.source else "")]
    if piece.channels:
        shown = ", ".join(piece.channels[:8])
        out.append(f"channels  {len(piece.channels)}: {shown}"
                   + ("..." if len(piece.channels) > 8 else ""))
    if piece.px_um:
        out.append(f"pixel  {piece.px_um} um")
    if piece.transform:
        out.append(f"in  {piece.space}  as {piece.transform}")
    if piece.annotates:
        out.append(f"annotates  {piece.annotates} on {piece.instance_key}")
    if piece.note:
        out.append(f"!  {piece.note}")
    return out


# The reader.
class _Stores:
    """Every metadata read the preview makes, batched and counted. Governance is checked first."""

    def __init__(self, source=None):
        from clinicalomx.extract.source import default_source

        self._src = source if source is not None else default_source()
        self._uri: dict[str, str] = {}
        self._denied: set[str] = set()
        self._resolved: set[str] = set()
        self._opened: dict[tuple[str, str | None], object] = {}
        # One geometry read per parquet, shared across every ROI that names it.
        self._frames: dict[str, object] = {}
        self._geometry: dict[str, object] = {}
        self.reads = 0

    def resolve(self, keys) -> None:
        from clinicalomx.governance import permits_read

        wanted = [k for k in dict.fromkeys(keys) if k and k not in self._resolved]
        if not wanted:
            return
        self._resolved.update(wanted)
        readable = []
        for key in wanted:
            if permits_read(key):
                readable.append(key)
            else:
                self._denied.add(key)
        if not readable:
            return
        self.reads += len(readable)
        for row in self._src.artifacts(readable):
            if row.uri:
                self._uri[row.artifact_id] = row.uri

    def uri(self, key: str | None) -> str | None:
        """Where a resolved key's bytes are, or ``None`` if nothing resolved it."""
        return self._uri.get(key) if key else None

    def frame(self, path):
        """The coordinate frame of a geometry parquet, read once per file."""
        if str(path) not in self._frames:
            self.reads += 1
            self._frames[str(path)] = geo.read_frame(path)
        return self._frames[str(path)]

    def geometry(self, path):
        """The geometry table itself, read once per file. Rows are looked up in memory after."""
        if str(path) not in self._geometry:
            self._geometry[str(path)] = geo.read_geometry(path)
        return self._geometry[str(path)]

    def open(self, key: str, variable: str | None):
        """``(Opened, "")`` or ``(None, why not)``. Never raises."""
        from clinicalomx.extract.builders.imaging import open_image

        if key in self._denied:
            return None, "governance forbids reading these bytes"
        if key not in self._uri:
            return None, "no registered Artifact resolves this key to a path"
        cached = self._opened.get((key, variable))
        if cached is not None:
            return cached, ""
        self.reads += 1
        try:
            opened = open_image(self._uri[key], variable=variable)
        except ImportError as exc:
            return None, (f"this environment has no reader for it ({exc}). Dimensions need "
                          f"`clinicalomx[extract]`; planning never does.")
        except Exception as exc:  # noqa: BLE001 - an unreadable store is a reported state
            return None, f"{type(exc).__name__}: {exc}"
        self._opened[(key, variable)] = opened
        return opened, ""


def _cyx(opened, box) -> tuple[tuple[int, int, int] | None, str]:
    """``((c, y, x), "")`` the crop would produce, or ``(None, why not)``."""
    n_c, why = channels_of(opened)
    if why:
        return None, why
    height, width = opened.shape_yx
    if box is None:
        return (n_c, height, width), ""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return None, (f"the crop box ({x0}, {y0}, {x1}, {y1}) falls outside the {width}x{height} "
                      f"source, so this element will FAIL to build here")
    return (n_c, y1 - y0, x1 - x0), ""


def _written_dtype(dtype) -> str:
    """The dtype the OME-TIFF writer will store."""
    return written(dtype)


def _bounds_for(record, values, recipe, snapshot, stores, roi_type: str):
    """``(bounds, why not)`` for one ROI, from the recipe's own placement."""
    dotted = recipe.placement_for(roi_type)
    if not dotted:
        return None, f"no placement is declared for a {roi_type}, so it is skipped at build time"
    placement = resolve_dotted(dotted)()

    row = frame = None
    keys = (snapshot.artifact_keys(record, FEATURES.SOURCE_ARTIFACT)
            or snapshot.artifact_keys(record, FEATURES.ROI_PARQUET))
    if keys:
        stores.resolve(keys)
        path = stores.uri(sorted(keys)[-1])
        if path is not None:
            try:
                frame = stores.frame(path)
                row = geo.row_for(stores.geometry(path), roi_name=str(record.name),
                                  roi_id=values.get(FEATURES.ROI_ID),
                                  parquet_row=values.get("parquet_row"))
            except Exception as exc:  # noqa: BLE001 - unplaceable is a state, not a failure
                return None, f"{type(exc).__name__}: {exc}"
    try:
        return placement.bounds(values=values, geometry_row=row,
                                container_attrs={"frame": frame}), ""
    except PlacementError as exc:
        return None, str(exc)


@dataclass(frozen=True)
class _Shaped:
    """A shape and dtype stand-in for the array ``to_ome`` reads."""

    shape: tuple[int, ...]
    dtype: object


# Per target.
def _series_pieces(unit, recipe, elements, record, values, snapshot, stores, *, bounds):
    """One :class:`Piece` per OME-TIFF series, in the recipe's declaration order."""
    wanted = [e for e in recipe.elements if e.name in elements]
    sources = {e.name: _one_key(snapshot, record, elements[e.name]) for e in wanted}
    stores.resolve(sources.values())

    pieces, meta = [], ChannelMeta()
    for element in wanted:
        role, key = elements[element.name], sources[element.name]
        opened, why = (stores.open(key, element.variable) if key
                       else (None, "this ROI carries no artifact under that role"))
        shape, note = (None, why)
        if opened is not None:
            height, width = opened.shape_yx
            box = (crop_box(bounds, width=width or None, height=height or None)
                   if bounds is not None else None)
            shape, note = _cyx(opened, box)
            if opened.meta.n and not meta.n:
                meta = opened.meta
        pieces.append(Piece(
            key=element.name, slot="series", element=element.name, index=len(pieces),
            role=role, variable=element.variable, source=key,
            dims=("c", "y", "x") if shape else (),
            shape=tuple(shape) if shape else (),
            dtype=_written_dtype(opened.array.dtype) if shape else "",
            channels=tuple(opened.meta.names) if opened is not None else (),
            px_um=opened.meta.px_um if opened is not None else None,
            size_bytes=_bytes_of(shape, _written_dtype(opened.array.dtype)) if shape else 0,
            note=note))
    return pieces, meta


def _ome_layout(plan, unit, recipe, record, values, snapshot, stores) -> tuple:
    """A single-ROI OME-TIFF: ``ometiff.build_core``'s series list, measured."""
    notes = []
    bounds, why = _bounds_for(record, values, recipe, snapshot, stores, _type_of(record))
    if bounds is None:
        notes.append(f"the crop box is unknown, so the series are reported at their SOURCE size: "
                     f"{why}")
    pieces, meta = _series_pieces(unit, recipe, unit.elements, record, values, snapshot, stores,
                                  bounds=bounds)

    shapes = {p.shape[1:] for p in pieces if p.shape}
    if len(shapes) > 1:
        notes.append(
            f"the series are not pixel-registered - {sorted(shapes)}. They are meant to be the "
            f"same field of view, so the build will REFUSE this unit rather than write it.")
    if not meta.n and pieces:
        notes.append("no source records channel names, so the file's channels will be "
                     "Channel:0..n unless the QptiffChannels Records supply them at build time.")
    return pieces, notes, meta


def _mosaic_layout(plan, unit, recipe, parent, recipe_children, snapshot, stores) -> tuple:
    """A merged OME-TIFF: one canvas per series, sized from the whole grid.

    Reports the canvas as ``Piece.size_bytes`` and the stored file, every level, as
    ``Piece.stored_bytes``.
    """
    from clinicalomx.extract.builders.mosaic import (
        MosaicError,
        canvas_shape,
        check_footprint,
        pyramid_levels,
        stored_bytes,
        union_bounds,
    )

    downsample = int((plan.request or {}).get("downsample", 1))
    notes, boxes, unplaceable = [], [], []
    for child in recipe_children:
        name = str(child.name)
        if name not in unit.child_elements:
            continue
        bounds, why = _bounds_for(child, snapshot.values(child), recipe, snapshot, stores,
                                  type_name_of(child))
        if bounds is None:
            unplaceable.append(f"{name.rsplit(':', 1)[-1]} ({why})")
            continue
        # The padded box, as `ometiff._child_targets` unions it.
        boxes.append(crop_box(bounds))

    # One representative core gives the channel count and dtype.
    sample = next((c for c in recipe_children if str(c.name) in unit.child_elements), None)
    pieces, meta = ([], None)
    if sample is not None:
        # Images only: a TIFF series is an array.
        images = {label: role for label, role in unit.child_elements[str(sample.name)].items()
                  if (recipe.element(label) or _NO_ELEMENT).kind == "image"}
        pieces, meta = _series_pieces(unit, recipe, images, sample, snapshot.values(sample),
                                      snapshot, stores, bounds=None)
    if not boxes:
        notes.append("no core can be placed, so there is nothing to make a canvas from and the "
                     "build will refuse this unit.")
        return pieces, notes, meta

    bounds = union_bounds(boxes)
    x0, y0, _x1, _y1 = bounds
    out, levels, base = [], {}, {}
    for piece in pieces:
        shape = canvas_shape(bounds, channels=piece.shape[0] if piece.shape else 1,
                             downsample=downsample) if piece.shape else ()
        note, stored = piece.note, 0
        if shape:
            levels[piece.key] = pyramid_levels(bounds, downsample=downsample)
            base[piece.key] = stored_bytes(boxes, piece.dtype, channels=shape[0],
                                           downsample=downsample, bounds=bounds)
            try:
                stored = check_footprint(boxes, piece.dtype, channels=shape[0],
                                         downsamples=levels[piece.key], bounds=bounds)
            except MosaicError as exc:
                note = str(exc)
        out.append(_replace(piece, shape=tuple(shape), n_from=len(boxes),
                            size_bytes=_bytes_of(shape, piece.dtype), stored_bytes=stored,
                            note=note.strip(), dims=("c", "y", "x") if shape else ()))

    sampled = (f" Channel count and dtype are sampled from "
               f"{str(sample.name).rsplit(':', 1)[-1]}." if sample is not None else "")
    # The percentage is the base level over the base canvas.
    measured = next((p for p in out if p.shape and p.stored_bytes and p.size_bytes
                     and base.get(p.key)), None)
    notes.append(f"the canvas is the union of {len(boxes)} placed core(s)' PADDED crop boxes, at "
                 f"origin ({x0}, {y0}) in full-resolution slide pixels"
                 + (f", downsampled {downsample}x" if downsample > 1 else "")
                 + (f", and the tiles the cores touch at the BASE level are "
                    f"{100 * base[measured.key] / measured.size_bytes:.0f}% of it"
                    if measured else "")
                 + ". The gaps are written as ABSENT tiles"
                 + (", so the first size on each series is the canvas and the second is what the "
                    "whole file holds" if any(p.size_bytes and p.stored_bytes for p in out) else "")
                 + "." + sampled)
    if any(len(f) > 1 for f in levels.values()):
        over = (f", which on {measured.key} is {_human(measured.stored_bytes - base[measured.key])}"
                f" above a {_human(base[measured.key])} base" if measured else "")
        notes.append(f"the file is PYRAMIDAL: {_levels_phrase(levels)} are written above the "
                     f"base so a viewer can open it, and the second size on each series counts "
                     f"them{over}. The base level is unchanged.")
    if unplaceable:
        notes.append(f"{len(unplaceable)} core(s) contribute nothing because nothing says where "
                     f"they belong: {', '.join(unplaceable[:5])}")
    return out, notes, meta


def _spatialdata_layout(plan, unit, recipe, record, values, snapshot, stores,
                        recipe_children) -> tuple:
    """A SpatialData: one element per slot, keyed as the builder keys them."""
    space, notes = recipe.coordinate_space, []
    pieces: list[Piece] = []

    # The anchor's own `self` elements; descendants are listed below.
    own = _at_level(unit.elements, recipe, "self" if recipe.layout == "merged" else None)
    measured, why = _measure_elements(own, recipe, record, values, snapshot, stores)
    notes.extend(why)
    pieces.extend(_sdata_pieces(own, recipe, measured, space,
                                key=lambda name: name, order=_placement_order))

    if recipe.layout != "merged":
        return pieces, notes, None

    taken: set[str] = set()
    sample = next((c for c in recipe_children if str(c.name) in unit.child_elements), None)
    for child in recipe_children:
        name, roi_type = str(child.name), type_name_of(child)
        elements = unit.child_elements.get(name)
        if not elements:
            continue
        keys = {label: key_for(unit.roi_name, name, roi_type, label, taken=taken)
                for label in elements}
        found: dict = {}
        if child is sample:
            found, why = _measure_elements(elements, recipe, child, snapshot.values(child),
                                           snapshot, stores)
            notes.extend(why)
        pieces.extend(_sdata_pieces(elements, recipe, found, space,
                                    key=lambda label, keys=keys: keys[label],
                                    order=_placement_order))
    if sample is not None:
        notes.append(f"dimensions are read from {str(sample.name).rsplit(':', 1)[-1]} only; every "
                     f"other descendant is listed by key, which is what the object will hold.")
    return pieces, notes, None


def _at_level(elements: dict, recipe, level: str | None) -> dict:
    """``elements`` narrowed to one declared level. ``None`` keeps the lot."""
    if level is None:
        return dict(elements)
    return {name: role for name, role in elements.items()
            if (recipe.element(name) or _NO_ELEMENT).level == level}


def _measure_elements(elements, recipe, record, values, snapshot, stores):
    """``({element: (Opened, shape, box, why)}, notes)`` for the elements at one level."""
    bounds = None
    wants_bounds = any((recipe.element(n) or _NO_ELEMENT).needs_features for n in elements)
    notes = []
    if wants_bounds:
        bounds, why = _bounds_for(record, values, recipe, snapshot, stores, _type_of(record))
        if bounds is None:
            notes.append(f"{str(record.name).rsplit(':', 1)[-1]}: the crop box is unknown, so "
                         f"cropped elements are reported at their SOURCE size ({why})")

    sources = {label: _one_key(snapshot, record, role) for label, role in elements.items()}
    stores.resolve(sources.values())

    out = {}
    for label, key in sources.items():
        element = recipe.element(label)
        variable = element.variable if element else None
        opened, why = (stores.open(key, variable) if key
                       else (None, "this ROI carries no artifact under that role"))
        box = shape = None
        if opened is not None:
            height, width = opened.shape_yx
            if bounds is not None and element is not None and element.needs_features:
                box = crop_box(bounds, width=width or None, height=height or None)
            shape, why = _cyx(opened, box)
        out[label] = (opened, shape, box, key, why)
    return out, notes


def _sdata_pieces(elements, recipe, measured, space, *, key, order) -> list[Piece]:
    entries = {label: ((recipe.element(label) or _NO_ELEMENT).kind, label) for label in elements}
    labels_key = next((key(label) for label, (kind, _l) in entries.items() if kind == "labels"),
                      None)
    out = []
    for label, (kind, _l) in order(entries.items()):
        element = recipe.element(label)
        opened, shape, box, source, why = measured.get(label, (None, None, None, None, ""))
        note = why
        if shape and kind == "image" and box is None and element is not None \
                and not element.needs_features:
            note = ("loaded WHOLE, not cropped: this element declares no placement features, so "
                    "the builder reads all of it into memory")
        out.append(Piece(
            key=key(label), slot=_SLOT.get(kind, kind), element=label, index=len(out),
            role=elements[label], variable=element.variable if element else None, source=source,
            dims=("c", "y", "x") if shape and kind == "image" else (("y", "x") if shape else ()),
            shape=tuple(shape[1:] if shape and kind == "labels" else shape or ()),
            dtype=str(opened.array.dtype) if shape else "",
            channels=tuple(opened.meta.names) if opened is not None and kind == "image" else (),
            px_um=opened.meta.px_um if opened is not None else None,
            size_bytes=_bytes_of(shape, str(opened.array.dtype)) if shape else 0,
            space=space,
            transform=(f"Translation(x={box[0]}, y={box[1]})" if box else "Identity"),
            annotates=labels_key if kind == "table" else None,
            instance_key=(element.instance_key if element and kind == "table" else None),
            note=note))
    return out


#: SpatialData's plural slot names for each element kind.
_SLOT = {"image": "images", "labels": "labels", "shapes": "shapes", "table": "tables",
         "points": "points"}


class _NoElement:
    """Stands in for an element the recipe does not declare."""

    kind = "image"
    level = "self"
    variable = None
    instance_key = None
    needs_features = ()


_NO_ELEMENT = _NoElement()


# Entry point.
def layout_of(plan, *, snapshot=None, source=None, measure: bool = True) -> TargetLayout:
    """The object ``plan`` would produce. Reads metadata, never a pixel.

    ``measure=False`` reads nothing and returns the structure alone.
    """
    snap = snapshot if snapshot is not None else Snapshot()
    try:
        recipe = _recipe(plan.recipe)
    except AssemblyError as exc:
        return TargetLayout(plan.target, plan.layout, plan.recipe, len(plan.buildable),
                            notes=(str(exc),))

    unit = next(iter(plan.buildable), None)
    if unit is None:
        return TargetLayout(
            plan.target, plan.layout, plan.recipe, 0, absences=_absences(plan, recipe, []),
            notes=("this plan builds nothing, so there is no object to describe.",))

    record = snap.record_by_uid(unit.roi_uid)
    if record is None:
        return TargetLayout(plan.target, plan.layout, plan.recipe, len(plan.buildable),
                            output=unit.output, absences=_absences(plan, recipe, []),
                            notes=("the ROI Record has gone since the plan was made.",))

    children = recipe.targets_under(record, snap) if recipe.layout == "merged" else []
    absences = _absences(plan, recipe, children)
    stores = _Stores(source) if measure else None
    if not measure:
        pieces, notes, meta = _structure_only(unit, recipe, children, snap)
    elif plan.target == "OME-TIFF" and recipe.layout == "merged":
        pieces, notes, meta = _mosaic_layout(plan, unit, recipe, record, children, snap, stores)
    elif plan.target == "OME-TIFF":
        pieces, notes, meta = _ome_layout(plan, unit, recipe, record, snap.values(record),
                                          snap, stores)
    elif plan.target == "SpatialData":
        pieces, notes, meta = _spatialdata_layout(plan, unit, recipe, record, snap.values(record),
                                                  snap, stores, children)
    else:
        pieces, notes, meta = _structure_only(unit, recipe, children, snap)
        notes.append(
            f"no layout is known for target {plan.target!r}, so this is the element list and not "
            f"the object's internal structure. The wiring view is the honest one here.")

    xml, why = _ome_xml(pieces, meta) if plan.target == "OME-TIFF" else ("", "")
    if why:
        notes.append(why)
    return TargetLayout(
        target=plan.target, layout=plan.layout, recipe=plan.recipe,
        n_outputs=len(plan.buildable), output=unit.output,
        measured_on=str(record.name) if measure else "",
        pieces=tuple(pieces), absences=absences,
        reads=stores.reads if stores is not None else 0,
        notes=tuple(notes), ome_xml=xml)


def _structure_only(unit, recipe, children, snapshot) -> tuple:
    """The object's shape with no read at all."""
    slot = (lambda kind: "series") if recipe.target == "OME-TIFF" else (
        lambda kind: _SLOT.get(kind, kind))
    own = _at_level(unit.elements, recipe, "self" if recipe.layout == "merged" else None)
    pieces = [
        Piece(key=name, slot=slot((recipe.element(name) or _NO_ELEMENT).kind), element=name,
              index=i, role=role,
              variable=(recipe.element(name) or _NO_ELEMENT).variable,
              space=recipe.coordinate_space if recipe.target == "SpatialData" else "")
        for i, (name, role) in enumerate(own.items())]
    taken: set[str] = set()
    for child in children:
        name, roi_type = str(child.name), type_name_of(child)
        for label, role in unit.child_elements.get(name, {}).items():
            element = recipe.element(label) or _NO_ELEMENT
            pieces.append(Piece(
                key=key_for(unit.roi_name, name, roi_type, label, taken=taken),
                slot=slot(element.kind), element=label, index=len(pieces), role=role,
                variable=element.variable,
                space=recipe.coordinate_space if recipe.target == "SpatialData" else ""))
    return pieces, ["dimensions, channels and dtype were not read."], None


def _absences(plan, recipe, children) -> tuple[Absence, ...]:
    """What the output will not have, counted over every unit and every child.

    The denominator is ``Recipe.targets_under``, so a child that contributes nothing still counts.
    """
    states = {e.element: e.state for e in (plan.availability.recipes[0].elements
                                           if plan.availability.recipes else ())}
    if plan.blocked:
        return tuple(
            Absence(name, "nothing will be built at all: this element blocks the whole plan.",
                    states.get(name, ""))
            for name in plan.blocked)

    units = plan.buildable
    merged = recipe.layout == "merged"
    if merged:
        fanout = units[0].child_elements
        total = len(children) or len(fanout)
        contributing = [c for c in (str(c.name) for c in children) if c in fanout] or list(fanout)
    else:
        fanout = {u.roi_name: u.elements for u in units}
        total = len(units)
        contributing = list(fanout)

    filled: dict[str, int] = {}
    for elements in fanout.values():
        for label in elements:
            filled[label] = filled.get(label, 0) + 1

    out = []
    if merged and total > len(contributing):
        out.append(Absence(
            "(whole ROIs)",
            f"{total - len(contributing)} of {total} ROIs contribute no element at all and will "
            f"not appear in the object.", "", (len(contributing), total)))
    for element in recipe.elements:
        if merged and element.level != "children":
            if element.name not in units[0].elements:
                out.append(Absence(element.name, _effect(recipe, element.name, 0, 1),
                                   states.get(element.name, "")))
            continue
        got = filled.get(element.name, 0)
        if got < total:
            out.append(Absence(element.name, _effect(recipe, element.name, got, total),
                               states.get(element.name, ""), (got, total) if got else None))
    return tuple(out)


def _effect(recipe, element: str, got: int, total: int) -> str:
    """One sentence about the output."""
    kind = (recipe.element(element) or _NO_ELEMENT).kind
    thing = "series" if recipe.target == "OME-TIFF" else f"{kind} element"
    if got == 0:
        return (f"the object will have no {thing} named {element!r}."
                if total <= 1 else
                f"no ROI can supply it, so the object will have no {thing} named {element!r}.")
    if recipe.aggregates:
        return (f"the {element!r} {thing} is composited from {got} of {total} ROIs; nothing is "
                f"pasted where the other {total - got} would be, so the canvas stays empty there.")
    if recipe.layout == "merged":
        return (f"{got} of {total} ROIs contribute a {thing} here; the other {total - got} will "
                f"have no {element!r} in the object.")
    return (f"{got} of {total} outputs will carry the {element!r} {thing}; the other "
            f"{total - got} will not.")


def _ome_xml(pieces, meta) -> tuple[str, str]:
    """The OME model the file will carry, built by the builder's own ``to_ome``."""
    series = [(p.key, _Shaped(p.shape, _np_dtype(p.dtype)), p.dtype)
              for p in pieces if p.shape and p.slot == "series"]
    if not series:
        return "", ""
    try:
        from ome_types import to_xml

        from clinicalomx.extract.builders.ometiff import to_ome
    except ImportError as exc:
        return "", (f"the OME model needs `clinicalomx[extract]` ({exc}); the series table above "
                    f"is what this environment can say.")
    return to_xml(to_ome(series, meta or ChannelMeta())), ""


def _np_dtype(name: str):
    import numpy as np

    return np.dtype(name)


def _one_key(snapshot, record, role: str) -> str | None:
    """The newest-named artifact key this role would be read from here, or ``None``. Inherits."""
    keys = snapshot.artifact_keys(record, role, inherit=role in INHERITED_FEATURES)
    return sorted(keys)[-1] if keys else None


def _type_of(record) -> str:
    rtype = getattr(record, "type", None)
    return str(getattr(rtype, "name", rtype)) if rtype is not None else ""


def _bytes_of(shape, dtype) -> int:
    """Uncompressed bytes for one array of any rank."""
    import numpy as np

    if not shape or not dtype:
        return 0
    return int(np.prod([int(n) for n in shape])) * int(np.dtype(dtype).itemsize)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _replace(piece: Piece, **kw) -> Piece:
    return dataclasses.replace(piece, **kw)
