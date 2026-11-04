"""Locating a child ROI's row in a geometry parquet, by ``roi_id`` rather than position.

No pandas import.
"""
from __future__ import annotations

from clinicalomx.names import FEATURES

#: Column carrying the stable per-row identity; the same name as the feature.
ROI_ID_COLUMN = FEATURES.ROI_ID


def roi_id_for(record_name: str) -> str:
    """``<slide>:TMA0:R4C2`` -> ``R4C2``; ``<slide>:TMA0`` -> ``TMA0``; a slide -> its own name."""
    return record_name.rsplit(":", 1)[-1]


def geometry_row(gdf, *, roi_id: str | None = None, parquet_row: int | None = None):
    """The parquet row belonging to one child ROI.

    Matches ``roi_id``, else falls back to the positional ``parquet_row`` for older parquets.
    Raises ``LookupError`` rather than return a wrong row.
    """
    if roi_id is not None and ROI_ID_COLUMN in getattr(gdf, "columns", ()):
        hits = gdf[gdf[ROI_ID_COLUMN] == roi_id]
        if len(hits) == 1:
            return hits.iloc[0]
        if len(hits) > 1:
            raise LookupError(
                f"roi_id {roi_id!r} matches {len(hits)} rows; the parquet is ambiguous"
            )
        # The column exists but predates this child: fall through.

    if parquet_row is None:
        raise LookupError(f"no roi_id match for {roi_id!r} and no parquet_row to fall back on")
    if not 0 <= int(parquet_row) < len(gdf):
        raise LookupError(f"parquet_row {parquet_row} out of range for {len(gdf)} row(s)")
    return gdf.iloc[int(parquet_row)]
