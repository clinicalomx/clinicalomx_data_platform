"""GeoParquet IO that preserves ``GeoDataFrame.attrs``.

pandas drops ``.attrs`` on ``to_parquet``, and ``attrs["scale_factor"]`` maps thumbnail px to full
resolution. These helpers store ``.attrs`` in the parquet schema metadata.

Heavy imports are deferred, so the module imports at ``prefect deploy`` time with only prefect.
"""

from __future__ import annotations

_ATTRS_KEY = b"roi_attrs"


def write_roi_parquet(gdf, path) -> None:
    """Write ``gdf`` to GeoParquet at ``path``, storing ``gdf.attrs`` as file metadata."""
    import json

    import pyarrow.parquet as pq

    path = str(path)
    gdf.to_parquet(path)
    if not gdf.attrs:
        return
    table = pq.read_table(path)
    meta = dict(table.schema.metadata or {})
    meta[_ATTRS_KEY] = json.dumps(gdf.attrs).encode()
    pq.write_table(table.replace_schema_metadata(meta), path)


def read_roi_parquet(path):
    """Read a GeoParquet written by :func:`write_roi_parquet`, restoring ``.attrs``."""
    import json

    import geopandas as gpd
    import pyarrow.parquet as pq

    path = str(path)
    gdf = gpd.read_parquet(path)
    raw = (pq.read_schema(path).metadata or {}).get(_ATTRS_KEY)
    if raw:
        gdf.attrs.update(json.loads(raw))
    return gdf
