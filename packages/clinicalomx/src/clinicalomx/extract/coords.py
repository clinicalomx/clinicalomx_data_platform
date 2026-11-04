"""Where an ROI sits in its parent's full-resolution pixels. Every path fails loudly.

:class:`GeometryPlacement` places Cores from their dearray parquet geometry. :class:`BboxPlacement`
places TMAs and tissue sections from ``bbox_*`` features. :func:`crop_box` reproduces the bgnorm
stage's padded crop, so raw and bgnormed crops are pixel-registered.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from clinicalomx.extract import is_inferred_cell

#: The padding the bgnorm stage applies before cropping a core (``bgnorm_stage._crop_full_res``).
#: Changing it misregisters every extract from its bgnormed image.
PAD_RATIO = 1.1

#: GeoParquet metadata key for the ratio between geometry pixels and full resolution.
SCALE_FACTOR = "scale_factor"


class PlacementError(RuntimeError):
    """A position could not be established. Never replaced by a default."""


def crop_box(bounds, *, pad_ratio: float = PAD_RATIO, width: int | None = None,
             height: int | None = None) -> tuple[int, int, int, int]:
    """``(x0, y0, x1, y1)`` integer pixels, padded and clamped, exactly as bgnorm crops.

    Pads by ``(pad_ratio - 1) * max(w, h) / 2`` on every side, floors the starts, ceils the stops,
    and clamps to the image when its size is known.
    """
    x0, y0, x1, y1 = (float(v) for v in bounds)
    if pad_ratio != 1.0:
        # Matches `_crop_full_res` including its float error. Do not round: it shifts the crop
        # one pixel off the bgnormed image. See tests/test_coords.py.
        margin = (pad_ratio - 1.0) * max(x1 - x0, y1 - y0) / 2.0
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    X0, X1 = int(math.floor(x0)), int(math.ceil(x1))
    Y0, Y1 = int(math.floor(y0)), int(math.ceil(y1))
    if width is not None:
        X0, X1 = max(0, X0), min(int(width), X1)
    if height is not None:
        Y0, Y1 = max(0, Y0), min(int(height), Y1)
    return X0, Y0, X1, Y1


@runtime_checkable
class Placement(Protocol):
    """Maps an ROI's own metadata to bounds in its recipe's coordinate space."""

    #: Must equal ``Recipe.coordinate_space``; ``assemble`` checks it before building.
    coordinate_space: str

    def bounds(self, *, values: Mapping[str, Any], geometry_row: Any = None,
               container_attrs: Mapping[str, Any] | None = None,
               ) -> tuple[float, float, float, float]:
        """``(x1, y1, x2, y2)`` in full-resolution pixels, unpadded."""
        ...


class PlacementSet:
    """The placements a recipe declared, keyed by ROI subtype.

    :meth:`for_roi` returns ``None`` where no declaration covers a subtype; the caller must skip
    that ROI and report it. ``bounds`` forwards to the default placement.
    """

    def __init__(self, by_type: dict[str, Any]):
        self._by_type = by_type

    @property
    def default(self):
        return self._by_type.get("") or next(iter(self._by_type.values()), None)

    def for_roi(self, roi_type: str):
        return self._by_type.get(roi_type) or self._by_type.get("")

    @property
    def coordinate_space(self) -> str | None:
        spaces = {getattr(p, "coordinate_space", None) for p in self._by_type.values()}
        return spaces.pop() if len(spaces) == 1 else None

    def spaces(self) -> dict[str, Any]:
        return {k: getattr(p, "coordinate_space", None) for k, p in self._by_type.items()}

    def bounds(self, **kw):
        if self.default is None:
            raise PlacementError("this recipe declares no placement at all")
        return self.default.bounds(**kw)


#: Largest disagreement, in full-resolution pixels, between a metadata strategy and the recorded
#: origin before it is reported.
EVIDENCE_TOLERANCE_PX = 1.0


@dataclass(frozen=True)
class Placed:
    """Bounds, and how they were arrived at."""

    bounds: tuple[float, float, float, float]
    #: ``"evidence"`` or the placement class name.
    source: str
    #: ``{strategy: bounds or the reason it declined}``.
    verdicts: dict[str, str] = field(default_factory=dict)
    #: Metadata strategies that disagreed with the evidence beyond the tolerance.
    disagreements: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"bounds": list(self.bounds), "source": self.source,
                "verdicts": dict(self.verdicts), "disagreements": list(self.disagreements)}


def cross_check(evidence, candidates: Mapping[str, Any],
                *, tolerance: float = EVIDENCE_TOLERANCE_PX) -> Placed:
    """Place from the evidence and report which metadata strategies disagree with it.

    ``candidates`` maps a strategy name to its bounds or to the reason it declined.
    """
    verdicts: dict[str, str] = {}
    disagreements = []
    for name, result in candidates.items():
        if not isinstance(result, tuple):
            verdicts[name] = f"declined: {result}"
            continue
        verdicts[name] = f"({result[0]:.0f}, {result[1]:.0f}, {result[2]:.0f}, {result[3]:.0f})"
        if evidence is None:
            continue
        off = max(abs(a - b) for a, b in zip(result, evidence, strict=True))
        if off > tolerance:
            disagreements.append(name)
            verdicts[name] += f"  DISAGREES by {off:.0f}px"

    if evidence is not None:
        return Placed(evidence, "evidence", verdicts, tuple(disagreements))
    usable = [(n, r) for n, r in candidates.items() if isinstance(r, tuple)]
    if not usable:
        raise PlacementError(
            "no evidence and no metadata strategy could place this ROI: "
            + "; ".join(f"{n}: {v}" for n, v in verdicts.items()))
    return Placed(usable[0][1], usable[0][0], verdicts, ())


class GeometryPlacement:
    """A Core's position, from the shapely geometry in its dearray parquet.

    The caller passes the resolved geometry row. An unknown coordinate frame raises.
    """

    coordinate_space = "global"

    #: ROI-side features this needs. Recipes repeat these in ``needs_features``.
    NEEDS = ("source_artifact", "parquet_row")

    def __init__(self, *, pad_ratio: float = PAD_RATIO, coordinate_space: str | None = None):
        self.pad_ratio = pad_ratio
        if coordinate_space:
            self.coordinate_space = coordinate_space

    def bounds(self, *, values, geometry_row=None, container_attrs=None):
        # An inferred grid cell has no real box.
        if is_inferred_cell(values):
            raise PlacementError(
                "this grid cell was inferred by the dearray fit, not detected, so it has no real "
                "geometry and no background-normalised image. It is not a core that failed to "
                "process; it is not a core.")
        if geometry_row is None:
            raise PlacementError(
                "no geometry row for this ROI. Its dearray parquet is the only thing that says "
                "where it sits, and nothing else on the Record does.")

        geom = getattr(geometry_row, "geometry", None)
        if geom is None or getattr(geom, "is_empty", False):
            raise PlacementError(
                "the geometry row for this ROI is empty - an inferred or missing cell. There is "
                "no box to crop.")

        frame = (container_attrs or {}).get("frame")
        if frame is None:
            raise PlacementError(
                "the coordinate frame of this ROI's geometry is unknown, so its bounds cannot be "
                "mapped to full-resolution pixels. See extract.builders.geo.read_frame.")
        x1, y1, x2, y2 = frame.apply(geom.bounds)
        if x2 <= x1 or y2 <= y1:
            raise PlacementError(
                f"degenerate bounds ({x1:.0f}, {y1:.0f}, {x2:.0f}, {y2:.0f}): the box has no "
                f"area, so the crop would be empty rather than wrong - the same bug, caught "
                f"earlier.")
        return x1, y1, x2, y2


class BboxPlacement:
    """A TMA's or tissue section's position, from ``bbox_*`` features times a downsample ratio.

    Not for Cores, which have no ``bbox_*``. A missing scale factor raises; pass ``scale=1.0`` for
    full-resolution geometry. This ``scale_factor`` is a downsample ratio, not microns per pixel.
    """

    coordinate_space = "global"

    NEEDS = ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")

    def __init__(self, *, scale: float | None = None, coordinate_space: str | None = None):
        self._scale = scale
        if coordinate_space:
            self.coordinate_space = coordinate_space

    def _scale_factor(self, values, geometry_row, container_attrs) -> float:
        """The downsample ratio: the explicit ``scale``, else the parquet's frame metadata."""
        frame = (container_attrs or {}).get("frame")
        for source in (
            self._scale,
            getattr(frame, "to_fullres", None),
            getattr(geometry_row, "get", lambda _k, _d=None: None)(SCALE_FACTOR, None),
        ):
            if source is None:
                continue
            try:
                factor = float(source)
            except (TypeError, ValueError):
                continue
            if factor > 0:
                return factor
        raise PlacementError(
            f"no {SCALE_FACTOR} for this ROI. The bbox columns are in detection pixels and the "
            f"factor mapping them to full resolution is absent - pandas drops .attrs on "
            f"to_parquet, so it may have been lost when the geometry was written. Refusing to "
            f"assume 1.0: a wrong factor crops the wrong part of the slide and nothing "
            f"downstream would notice. Pass scale= explicitly if the geometry really is "
            f"full-resolution.")

    def bounds(self, *, values, geometry_row=None, container_attrs=None):
        missing = [c for c in self.NEEDS if values.get(c) is None]
        if missing:
            raise PlacementError(
                f"cannot position this ROI: {', '.join(missing)} carry no value. If this is a "
                f"Core, that is expected - the core objective never writes bbox features, so it "
                f"needs GeometryPlacement instead.")
        factor = self._scale_factor(values, geometry_row, container_attrs)
        x1, y1, x2, y2 = (float(values[c]) * factor for c in self.NEEDS)
        if x2 <= x1 or y2 <= y1:
            raise PlacementError(
                f"degenerate bounds ({x1}, {y1}, {x2}, {y2}): the box has no area, so the crop "
                f"would be empty rather than wrong - which is the same bug, caught earlier.")
        return x1, y1, x2, y2
