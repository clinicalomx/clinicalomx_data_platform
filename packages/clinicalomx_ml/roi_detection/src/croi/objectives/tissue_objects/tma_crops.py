"""Crop TMA regions out of registered thumbnail PNGs using a detection's ``bbox_thumb_xyxy``."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def extract_tma_crop_from_thumbnail(
    thumbnail_png_path: str | Path,
    bbox_thumb_xyxy: tuple[float, float, float, float],
) -> tuple[Image.Image, dict]:
    """Crop a TMA region out of a thumbnail PNG. Returns ``(PIL.Image, metadata)`` in the PNG's mode."""
    img = Image.open(thumbnail_png_path)
    w, h = img.size
    x1, y1, x2, y2 = bbox_thumb_xyxy
    # Clamp to image bounds; YOLO can overshoot by a pixel or two.
    x1 = max(0, int(x1)); y1 = max(0, int(y1))
    x2 = min(w, int(x2)); y2 = min(h, int(y2))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox after clamping: ({x1},{y1},{x2},{y2})")
    crop = img.crop((x1, y1, x2, y2))
    meta = {
        "thumbnail_size_yx": [h, w],
        "bbox_thumb_xyxy": [int(x1), int(y1), int(x2), int(y2)],
        "crop_size_yx": [crop.height, crop.width],
    }
    return crop, meta
