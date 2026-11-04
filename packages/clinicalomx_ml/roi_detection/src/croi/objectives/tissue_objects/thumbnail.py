"""Load a QPTIFF's scene-1 Thumbnail as a numpy array, the same way ingest does."""

from __future__ import annotations

from pathlib import Path

import bioio_qptiff
import numpy as np
from bioio import BioImage


def load_thumbnail(qptiff_path: str | Path) -> np.ndarray:
    """Scene-1 Thumbnail as ``(H, W)`` or ``(H, W, C)``, leading singleton dims squeezed."""
    image = BioImage(
        Path(qptiff_path),
        reader=bioio_qptiff.Reader,
        ome_metadata=False,
        include_aux_series=True,
    )
    image.reader.set_scene(1)
    if image.reader.current_scene != "Thumbnail":
        raise RuntimeError(
            f"expected scene 1 to be 'Thumbnail', got {image.reader.current_scene!r}"
        )
    arr = image.reader.xarray_dask_data.compute().values
    while arr.ndim > 3:
        arr = arr.squeeze(0)
    return arr