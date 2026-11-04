"""A QPTIFF's smallest pyramid level, projected and percentile-normalised to a uint8 image.

Reads with tifffile: bioio does not expose the pyramid.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile
from PIL import Image


def _smallest_pyramid_array(qptiff_path: Path) -> np.ndarray:
    """Lowest-resolution pyramid level of scene 0, or the only level when there is no pyramid."""
    with tifffile.TiffFile(qptiff_path) as tf:
        series = tf.series[0]
        levels = getattr(series, "levels", None)
        chosen = (levels[-1] if levels else series)
        return chosen.asarray()


def _move_channel_first(arr: np.ndarray) -> np.ndarray:
    """Put the channel axis first, taking the smallest dimension as channels."""
    if arr.ndim == 2:
        return arr[None, ...]
    if arr.ndim == 3:
        c_axis = int(np.argmin(arr.shape))
        if c_axis != 0:
            arr = np.moveaxis(arr, c_axis, 0)
        return arr
    while arr.ndim > 3 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim != 3:
        raise ValueError(f"unexpected pyramid array shape: {arr.shape}")
    return _move_channel_first(arr)


def _percentile_normalize_uint8(arr: np.ndarray, lo_pct: float = 1.0, hi_pct: float = 99.0) -> np.ndarray:
    lo, hi = np.percentile(arr, (lo_pct, hi_pct))
    if hi <= lo:
        hi = lo + 1
    return np.clip((arr.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


def qptiff_to_grayscale_array(
    qptiff_path: str | Path,
    target_max_edge: int | None = 1024,
    mode: str = "auto",
) -> tuple[np.ndarray, dict]:
    """Read a QPTIFF as ``(uint8 2D grayscale array, metadata)``.

    Projects channels per ``mode``, percentile-normalises (1st/99th) and Lanczos-downsamples so
    the longest edge is at most ``target_max_edge`` (never upsamples; None skips the resize).
    ``mode`` is "max", "mean", or "auto" (max above 4 channels, else mean).
    """
    arr = _smallest_pyramid_array(Path(qptiff_path))
    arr = _move_channel_first(arr)  # (C, Y, X)
    n_channels = arr.shape[0]

    resolved_mode = "max" if (mode == "auto" and n_channels > 4) else (
        "mean" if mode == "auto" else mode
    )
    if resolved_mode == "max":
        proj = arr.max(axis=0)
    elif resolved_mode == "mean":
        proj = arr.mean(axis=0)
    else:
        raise ValueError(f"mode must be 'max'/'mean'/'auto', got {mode!r}")

    norm = _percentile_normalize_uint8(proj)
    src_h, src_w = norm.shape

    if target_max_edge is not None and max(src_h, src_w) > target_max_edge:
        scale = target_max_edge / max(src_h, src_w)
        new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
        norm = np.array(Image.fromarray(norm, "L").resize(new_size, Image.LANCZOS))

    return norm, {
        "smallest_pyramid_size_yx": [int(src_h), int(src_w)],
        "n_channels": int(n_channels),
        "projection_mode": resolved_mode,
        "output_size_yx": [int(norm.shape[0]), int(norm.shape[1])],
    }


def qptiff_to_grayscale_image(
    qptiff_path: str | Path,
    target_max_edge: int | None = 1024,
    mode: str = "auto",
) -> tuple[Image.Image, dict]:
    """PIL-Image variant of `qptiff_to_grayscale_array`. Returns ('L'-mode Image, meta)."""
    arr, meta = qptiff_to_grayscale_array(qptiff_path, target_max_edge, mode)
    return Image.fromarray(arr, "L"), meta
