"""Vitessce wrapper that serves a QPTIFF as an OME-NGFF Zarr, read lazily with no conversion.

Pixels come from ``tifffile``'s native-tile zarr, so one served chunk decodes one native TIFF tile.
bioio supplies only the OME-NGFF metadata. Chunks are blosc-lz4 by default (``compressors=None``
for raw) and decoded tiles stay cached in memory.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from vitessce.wrappers import AbstractWrapper, make_repr, make_unique_filename
from zarr.codecs import BloscCodec

# zarr v3 chunk keys contain this separator; metadata keys do not.
_CHUNK_SEP = "/c/"
_INT_RE = re.compile(r"^\d+$")

_DEFAULT_COMPRESSORS = BloscCodec(cname="lz4", clevel=1)
_FALLBACK_TILE = 512  # when the native tile size is unknown


def _native_tile_size(img_path) -> int | None:
    """The QPTIFF's native square TIFF tile edge, or None when untiled or unreadable."""
    import tifffile

    try:
        with tifffile.TiffFile(str(img_path)) as tf:
            page = tf.series[0].levels[0].pages[0]
            tw, tl = int(getattr(page, "tilewidth", 0)), int(getattr(page, "tilelength", 0))
    except (OSError, IndexError, tifffile.TiffFileError):
        return None
    return min(tw, tl) if tw > 0 and tl > 0 else None


class QptiffZarrStore(zarr.storage.MemoryStore):
    """A zarr v3 MemoryStore that reads each QPTIFF chunk on first request.

    Metadata is written at construction; a missing chunk is read from the native tile, encoded,
    stored and returned.
    """

    def __init__(self, img_path=None, *, tile="auto", compressors=_DEFAULT_COMPRESSORS,
                 store_dict=None, read_only: bool = False):
        super().__init__(store_dict=store_dict, read_only=read_only)
        # State shared across read-only and writable clones.
        self._tile = 0
        self._tif = None          # open tifffile.TiffFile
        self._read_lock = None    # serialises tile reads on the shared handle
        self._levels: dict[str, dict[str, Any]] = {}
        self._async_arrays: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._filling: set[str] = set()  # keys mid-fill, breaking setitem's read-modify-write recursion
        self._empty: set[str] = set()    # all-fill-value chunks, served as a miss
        if img_path is None:
            return  # clone: with_read_only attaches state

        import tifffile
        from bioio_qptiff import Reader
        from bioio_qptiff.ome.zarr_writer import _build_ngff_zattrs

        # "auto" uses the native tile edge.
        self._tile = (_native_tile_size(img_path) or _FALLBACK_TILE) if tile == "auto" else int(tile)
        self._read_lock = threading.Lock()

        # bioio: OME-NGFF metadata only.
        reader = Reader(str(img_path))
        zattrs = _build_ngff_zattrs(reader.xarray_dask_datatree_data, reader.ome_metadata)

        # tifffile: the pixel source, a per-level group "0".."N" of natively tiled arrays.
        self._tif = tifffile.TiffFile(str(img_path))
        src_group = zarr.open(self._tif.series[0].aszarr(), mode="r")

        # Metadata only; array paths mirror "<scaleN>/image".
        root = zarr.create_group(store=self, overwrite=True)
        for name in sorted(
            (k for k in reader.xarray_dask_datatree_data.children if k.startswith("scale")),
            key=lambda k: int(k[len("scale"):]),
        ):
            src = src_group[str(int(name[len("scale"):]))]
            chunks = (1, min(self._tile, src.shape[1]), min(self._tile, src.shape[2]))
            grp = root.create_group(name)
            grp.create_array("image", shape=src.shape, chunks=chunks, dtype=src.dtype,
                             compressors=compressors)
            self._levels[f"{name}/image"] = {"src": src, "chunks": chunks, "shape": src.shape}
        root.attrs.update(zattrs)

    def with_read_only(self, read_only: bool = False) -> QptiffZarrStore:
        """Clone sharing the backing dict and lazy-fill state, for zarr's read-only and writable clones."""
        clone = QptiffZarrStore.__new__(QptiffZarrStore)
        zarr.storage.MemoryStore.__init__(clone, store_dict=self._store_dict, read_only=read_only)
        clone._tile = self._tile
        clone._tif = self._tif
        clone._read_lock = self._read_lock
        clone._levels = self._levels
        clone._async_arrays = self._async_arrays
        clone._locks = self._locks
        clone._filling = self._filling
        clone._empty = self._empty
        return clone

    async def get(self, key, prototype=None, byte_range=None):  # type: ignore[override]
        val = await super().get(key, prototype=prototype, byte_range=byte_range)
        if val is not None:
            return val
        target = self._chunk_target(key)
        if target is None:
            return None  # not a fillable chunk
        if key in self._empty or key in self._filling:
            # Empty chunk, or a re-entrant read from setitem: viv reads a miss as the fill value.
            return None
        await self._fill(key, *target)
        val = await super().get(key, prototype=prototype, byte_range=byte_range)
        if val is None:
            # zarr skips storing all-fill-value chunks; remember them.
            self._empty.add(key)
        return val

    def _chunk_target(self, key: str) -> tuple[str, tuple] | None:
        """Map a chunk key to ``(array_path, region_slices)``, or None if not a chunk."""
        if _CHUNK_SEP not in key:
            return None
        array_path, coord_str = key.split(_CHUNK_SEP, 1)
        level = self._levels.get(array_path)
        if level is None:
            return None
        coords = coord_str.split("/")
        if len(coords) != len(level["shape"]) or not all(_INT_RE.match(c) for c in coords):
            return None
        region = tuple(
            slice(int(i) * cs, min((int(i) + 1) * cs, dim))
            for i, cs, dim in zip(coords, level["chunks"], level["shape"])
        )
        return array_path, region

    async def _fill(self, key: str, array_path: str, region: tuple) -> None:
        """Read one native tile off-thread and write it into the store."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another coroutine may have filled this chunk while we waited.
            if await super().get(key, prototype=None) is not None:
                return
            src = self._levels[array_path]["src"]

            def _read():
                # tifffile's file handle is not safe for concurrent reads.
                with self._read_lock:
                    return np.asarray(src[region])

            data = await asyncio.get_running_loop().run_in_executor(None, _read)
            aa = await self._async_array(array_path)
            self._filling.add(key)
            try:
                await aa.setitem(region, data)
            finally:
                self._filling.discard(key)

    async def _async_array(self, array_path: str):
        aa = self._async_arrays.get(array_path)
        if aa is None:
            from zarr.api.asynchronous import open_array

            # Open a writable view; it shares the backing dict this store reads.
            aa = await open_array(store=self.with_read_only(False), path=array_path, mode="r+")
            self._async_arrays[array_path] = aa
        return aa


class QptiffWrapper(AbstractWrapper):
    """Wrap a QPTIFF for the spatialBeta / layerControllerBeta views as an ``image.ome-zarr``.

    Provide exactly one source:

    :param str img_path: local filepath to a ``.qptiff``.
    :param img_artifact: a lamindb Artifact for a ``.qptiff``.
    :param str img_url: URL of an already-served OME-Zarr.
    :param tile: served chunk edge, ``"auto"`` (native TIFF tile) or an int.
    :param compressors: zarr codec(s); default blosc-lz4, ``None`` for raw.
    :param list coordinate_transformations: column-major transform matrix (optional).
    :param dict coordination_values: optional coordinationValues for the file def.
    :param \\*\\*kwargs: inherited from :class:`~vitessce.wrappers.AbstractWrapper`.
    """

    def __init__(self, img_path=None, img_url=None, img_artifact=None, *, tile="auto",
                 compressors=_DEFAULT_COMPRESSORS,
                 coordinate_transformations=None, coordination_values=None, **kwargs):
        super().__init__(**kwargs)
        self._repr = make_repr(locals())
        if sum(x is not None for x in (img_path, img_url, img_artifact)) != 1:
            raise ValueError("Expected exactly one of img_path, img_url, or img_artifact")

        self._img_url = img_url
        self._coordinate_transformations = coordinate_transformations
        self._coordination_values = coordination_values
        self.local_dir_uid = make_unique_filename(".ome.zarr")

        self.is_remote = img_url is not None
        self.is_store = not self.is_remote
        self._store: QptiffZarrStore | None = None
        if not self.is_remote:
            local_path = img_artifact.cache() if img_artifact is not None else img_path
            self._local_path = Path(str(local_path))
            self._store = QptiffZarrStore(self._local_path, tile=tile, compressors=compressors)

    def convert_and_save(self, dataset_uid, obj_i, base_dir=None):
        if not self.is_remote:
            super().convert_and_save(dataset_uid, obj_i, base_dir=base_dir)
            self.register_zarr_store(dataset_uid, obj_i, self._store, self.local_dir_uid)
        self.file_def_creators.append(self.make_image_file_def_creator(dataset_uid, obj_i))
        self.routes += self.make_image_routes(dataset_uid, obj_i)

    def make_image_routes(self, dataset_uid, obj_i):
        # The store is served over the kernel RPC, so there are no HTTP routes.
        return []

    def get_img_url(self, base_url="", dataset_uid="", obj_i=""):
        if self.is_remote:
            return self._img_url
        return self._get_url(base_url, dataset_uid, obj_i, self.local_dir_uid)

    def make_image_file_def_creator(self, dataset_uid, obj_i):
        def image_file_def_creator(base_url):
            options = {}
            if self._coordinate_transformations is not None:
                options["coordinateTransformations"] = self._coordinate_transformations
            file_def = {
                "fileType": "image.ome-zarr",
                "url": self.get_img_url(base_url, dataset_uid, obj_i),
            }
            if options:
                file_def["options"] = options
            if self._coordination_values is not None:
                file_def["coordinationValues"] = self._coordination_values
            return file_def
        return image_file_def_creator
