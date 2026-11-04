"""Derive core fields of view on demand from Core ROI Records. Nothing is stored.

A ``Core`` Record's ``source_artifact`` is the cores GeoParquet, ``parquet_row`` its row, and
``source_image`` the source QPTIFF.

- :func:`core_crop` returns the full-resolution ``(c, y, x)`` DataArray, lazily.
- :func:`core_crop_datatree` and :func:`write_core_ome_zarr` crop every pyramid level and write
  an OME-NGFF zarr.
"""
from __future__ import annotations

import geopandas as gpd
import lamindb as ln

from croi.utils.image_preprocess import _largest_scale_image, crop_datatree_to_box


def _resolve_artifact(value):
    """Resolve an ``ln.Artifact`` feature value, which comes back as its key."""
    return ln.Artifact.get(key=value) if isinstance(value, str) else value


def _core_geometry(core):
    """``(geometry, feature_values)`` for a Core ROI. Raises for inferred cells.

    Geometry is in the parquet's ``croi:coordinate_space`` frame, the smallest pyramid level.
    """
    vals = core.features.get_values()
    cores_pq = _resolve_artifact(vals["source_artifact"])
    geom = gpd.read_parquet(cores_pq.path).geometry.iloc[int(vals["parquet_row"])]
    if geom is None or getattr(geom, "is_empty", False):
        raise ValueError(f"{core.name}: inferred/missing cell has no geometry to crop")
    return geom, vals


def _open_reader(vals):
    """Open the source QPTIFF's ``bioio_qptiff`` Reader from a Core's ``source_image``."""
    from bioio import BioImage
    from bioio_qptiff import Reader

    qptiff = _resolve_artifact(vals["source_image"])
    return BioImage(qptiff.path, reader=Reader).reader


def core_crop(core, *, pad_ratio: float = 1.1, ds=None, scene: str = "FullResolution"):
    """A Core's full-resolution field of view as a lazy ``(c, y, x)`` DataArray.

    Pass an open scene DataTree as ``ds`` to avoid reopening the QPTIFF per core.
    Raises for inferred cells (``is_detected=False``).
    """
    geom, vals = _core_geometry(core)
    if ds is None:
        ds = _open_reader(vals).xarray_dask_scene_datatree[scene]
    # The box is in the smallest-level frame; crop_datatree_to_box is scale-invariant.
    return crop_datatree_to_box(ds, geom.bounds, pad_ratio=pad_ratio, full_scale_only=True)


def _ome_for_crop(ome, cropped_dt):
    """Deep-copy ``ome`` with each image's ``SizeX``/``SizeY`` set to the crop's full-resolution size."""
    import copy

    full = _largest_scale_image(cropped_dt)
    ynm = next(d for d in full.dims if d.lower() == "y")
    xnm = next(d for d in full.dims if d.lower() == "x")
    h, w = int(full.sizes[ynm]), int(full.sizes[xnm])
    ome = copy.deepcopy(ome)
    for img in (getattr(ome, "images", None) or []):
        px = getattr(img, "pixels", None)
        if px is not None:
            px.size_x, px.size_y = w, h
    return ome


def core_crop_datatree(core, *, pad_ratio: float = 1.1, reader=None, scene: str = "FullResolution"):
    """Crop a Core's field of view across every pyramid level.

    Returns ``(datatree, ome)``, shaped as ``bioio_qptiff.ome.zarr_writer.write_ome_zarr`` expects,
    with OME sizes updated to the crop. Pass ``reader`` to reuse it across cores. ``scene`` must
    be the reader's active scene. Raises for inferred cells.
    """
    geom, vals = _core_geometry(core)
    if reader is None:
        reader = _open_reader(vals)
    ds = reader.xarray_dask_scene_datatree[scene]
    cropped = crop_datatree_to_box(ds, geom.bounds, pad_ratio=pad_ratio, full_scale_only=False)
    return cropped, _ome_for_crop(reader.ome_metadata, cropped)


def write_core_ome_zarr(
    core,
    store,
    *,
    pad_ratio: float = 1.1,
    reader=None,
    scene: str = "FullResolution",
    overwrite: bool = False,
    **write_kwargs,
):
    """Crop a Core's field of view and write it as an OME-NGFF zarr. Returns the root ``zarr.Group``.

    Extra kwargs go to ``write_ome_zarr``.

    Example::

        from croi.objectives.tma_core.crops import write_core_ome_zarr
        core = ln.Record.get(name="...:TMA0:R0C0")
        write_core_ome_zarr(core, "R0C0.ome.zarr", overwrite=True)
    """
    from bioio_qptiff.ome.zarr_writer import write_ome_zarr

    datatree, ome = core_crop_datatree(core, pad_ratio=pad_ratio, reader=reader, scene=scene)
    return write_ome_zarr(datatree, ome, store, overwrite=overwrite, **write_kwargs)
