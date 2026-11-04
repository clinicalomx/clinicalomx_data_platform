"""Reading a geometry parquet, and its coordinate frame.

Two producers write core geometry in different frames:

* ``cores_accepted`` - the human-approved grid, in full-resolution pixels, with ``roi_attrs``
  schema metadata (``scale_factor == 1.0``).
* ``core_shapes`` - the raw dearray grid, in smallest-pyramid-level pixels, with a
  ``croi:coordinate_space`` blob carrying the downsample factor.

:func:`read_frame` reads the parquet's schema metadata and raises when neither key is present.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clinicalomx.geometry import geometry_row, roi_id_for

#: Written by ``qptiff_pipeline.domain.roi_io.write_roi_parquet``. Full-resolution geometry.
ROI_ATTRS_KEY = b"roi_attrs"

#: Written by the detection repo's ``_write_geoparquet_with_meta``. Detection-frame geometry.
CROI_SPACE_KEY = b"croi:coordinate_space"


class FrameUnknown(RuntimeError):
    """The parquet does not say what frame its coordinates are in."""


@dataclass(frozen=True)
class Frame:
    """How a geometry parquet's coordinates relate to full resolution."""

    #: Multiply geometry bounds by this to reach full-resolution pixels.
    to_fullres: float
    #: Which metadata key established it.
    basis: str

    def apply(self, bounds) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = bounds
        f = self.to_fullres
        return x1 * f, y1 * f, x2 * f, y2 * f


def _decode(raw) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def read_metadata(path) -> dict[bytes, Any]:
    """The parquet's schema metadata, JSON-decoded where possible."""
    import pyarrow.parquet as pq

    meta = pq.read_schema(str(path)).metadata or {}
    return {k: (_decode(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in meta.items()}


def read_frame(path) -> Frame:
    """What frame this parquet's geometry is in. Raises :class:`FrameUnknown` when unstated."""
    meta = read_metadata(path)

    attrs = meta.get(ROI_ATTRS_KEY)
    if isinstance(attrs, dict) and attrs.get("scale_factor") is not None:
        # This `scale_factor` is a downsample ratio, not microns per pixel.
        return Frame(float(attrs["scale_factor"]), "roi_attrs")

    space = meta.get(CROI_SPACE_KEY)
    if isinstance(space, dict) and space.get("downsample_to_scale0") is not None:
        return Frame(float(space["downsample_to_scale0"]), "croi:coordinate_space")

    raise FrameUnknown(
        f"{path}: no {ROI_ATTRS_KEY.decode()} or {CROI_SPACE_KEY.decode()} metadata, so the "
        f"coordinate frame of this geometry is unknown. Refusing to assume full resolution: the "
        f"two frames this instance uses differ by the pyramid depth, and cropping with the wrong "
        f"one yields a plausible image of the wrong part of the slide, which nothing downstream "
        f"would catch. Rewrite the parquet with roi_io.write_roi_parquet to stamp its frame.")


def read_geometry(path):
    """``(frame, attrs)`` and the frame itself. Read as geo where the file declares a geometry."""
    import pyarrow.parquet as pq

    meta = pq.read_schema(str(path)).metadata or {}
    if b"geo" in meta:
        import geopandas as gpd

        return gpd.read_parquet(str(path))
    import pandas as pd

    return pd.read_parquet(str(path))


def has_geometry(path) -> bool:
    """Does this parquet carry a geometry column?"""
    import pyarrow.parquet as pq

    schema = pq.read_schema(str(path))
    return b"geo" in (schema.metadata or {}) or "geometry" in schema.names


def row_for(frame_df, *, roi_name: str, roi_id: str | None = None, parquet_row=None):
    """This ROI's row, through ``clinicalomx.geometry.geometry_row``, as bgnorm resolves it."""
    return geometry_row(frame_df, roi_id=roi_id or roi_id_for(roi_name),
                        parquet_row=parquet_row)
