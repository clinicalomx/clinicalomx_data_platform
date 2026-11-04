"""Render QPTIFF thumbnails with detection boxes overlaid, as JPEG, for visual QA."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from croi.objectives.tissue_objects.thumbnail import load_thumbnail


# TMA red, tissue green, grey otherwise.
_CLASS_COLORS: dict[int, tuple[int, int, int]] = {
    0: (255, 80, 80),
    1: (80, 255, 120),
}


def _to_rgb_uint8(arr: np.ndarray) -> Image.Image:
    """Normalize a 2D or 3D thumbnail array to an 8-bit RGB PIL Image."""
    if arr.dtype != np.uint8:
        lo, hi = np.percentile(arr, (1, 99))
        if hi <= lo:
            hi = lo + 1
        arr = np.clip((arr.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, "L").convert("RGB")
    if arr.ndim == 3 and arr.shape[-1] in (1, 3, 4):
        return Image.fromarray(arr).convert("RGB")
    # Unusual shape: keep the first slice of each extra dimension.
    while arr.ndim > 2:
        arr = arr[0]
    return Image.fromarray(arr, "L").convert("RGB")


def render_overlay(
    qptiff_path: str | Path,
    detections: list[dict],
    out_path: str | Path,
    max_size: int = 640,
    jpeg_quality: int = 70,
    title: str | None = None,
) -> None:
    """Draw ``detections`` (thumbnail pixels) onto the QPTIFF thumbnail and save as JPEG."""
    arr = load_thumbnail(qptiff_path)
    img = _to_rgb_uint8(arr)

    scale = min(1.0, max_size / max(img.size))
    if scale < 1:
        img = img.resize(
            (int(img.size[0] * scale), int(img.size[1] * scale)),
            Image.LANCZOS,
        )

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()

    for d in detections:
        x1, y1, x2, y2 = d["bbox_thumb_xyxy"]
        sb = [v * scale for v in (x1, y1, x2, y2)]
        color = _CLASS_COLORS.get(d["class_id"], (200, 200, 200))
        draw.rectangle(sb, outline=color, width=2)
        label = f"{d['class_name']} {d['confidence']:.2f}"
        draw.text((sb[0] + 2, sb[1] + 2), label, fill=color, font=font)

    if title:
        draw.text((4, 4), title, fill=(255, 255, 255), font=font)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=jpeg_quality, optimize=True)


def render_contact_sheet(
    items: Iterable[tuple[str, list[dict]]],
    out_path: str | Path,
    cols: int = 6,
    cell_px: int = 220,
    jpeg_quality: int = 75,
) -> None:
    """Tile per-image overlays into one JPEG. ``items`` yields ``(qptiff_path, detections)``."""
    items = list(items)
    if not items:
        return
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_px, rows * cell_px), (20, 20, 20))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 10)
    except OSError:
        font = ImageFont.load_default()

    for idx, (qptiff_path, detections) in enumerate(items):
        arr = load_thumbnail(qptiff_path)
        img = _to_rgb_uint8(arr)
        scale = cell_px / max(img.size)
        new_size = (round(img.size[0] * scale), round(img.size[1] * scale))
        img = img.resize(new_size, Image.LANCZOS)
        draw = ImageDraw.Draw(img)
        for d in detections:
            x1, y1, x2, y2 = d["bbox_thumb_xyxy"]
            sb = [v * scale for v in (x1, y1, x2, y2)]
            color = _CLASS_COLORS.get(d["class_id"], (200, 200, 200))
            draw.rectangle(sb, outline=color, width=2)
        stem = Path(qptiff_path).stem[:32]
        draw.text((2, 2), stem, fill=(255, 255, 255), font=font)
        cy = (idx // cols) * cell_px
        cx = (idx % cols) * cell_px
        sheet.paste(img, (cx + (cell_px - img.size[0]) // 2, cy + (cell_px - img.size[1]) // 2))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "JPEG", quality=jpeg_quality, optimize=True)


def render_contact_sheets(
    items: Iterable[tuple[str, list[dict]]],
    out_dir: str | Path,
    per_sheet: int = 36,
    cols: int = 6,
    cell_px: int = 220,
    jpeg_quality: int = 75,
) -> list[Path]:
    """Paginate items into sheets of ``per_sheet`` cells. Returns the paths."""
    items = list(items)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for page_idx, start in enumerate(range(0, len(items), per_sheet)):
        page = items[start : start + per_sheet]
        path = out_dir / f"contact_sheet_{page_idx:02d}.jpg"
        render_contact_sheet(page, path, cols=cols, cell_px=cell_px, jpeg_quality=jpeg_quality)
        written.append(path)
    return written


def render_detections_jsonl(
    jsonl: str | Path,
    out_dir: str | Path | None = None,
    *,
    min_conf: float = 0.0,
    max_size: int = 640,
    jpeg_quality: int = 70,
    per_sheet: int = 36,
    cols: int = 6,
    cell_px: int = 220,
    skip_empty: bool = False,
    per_image: bool = True,
    contact_sheets: bool = True,
) -> Path:
    """Render a detections JSONL to per-image JPEGs and paginated contact sheets.

    Nothing in this tree writes the JSONL. One JSON object per line::

        {"qptiff_uid": "<artifact uid>",      # or "qptiff_key": "<artifact key>"
         "detections": [{"class_id": 0, "class_name": "TMA", "confidence": 0.91,
                         "bbox_thumb_xyxy": [x1, y1, x2, y2]}]}

    ``qptiff_uid`` wins over ``qptiff_key``. Boxes are thumbnail pixels. Every detection key is
    required. Rows that do not resolve or render are reported and skipped. Returns the output dir.
    """
    import json

    import lamindb as ln

    jsonl = Path(jsonl)
    out_dir = Path(out_dir) if out_dir else jsonl.parent / "overlays" / jsonl.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(rows)} rows from {jsonl}")

    def _qptiff_path(row: dict) -> Path | None:
        try:
            if row.get("qptiff_uid"):
                return Path(ln.Artifact.get(uid=row["qptiff_uid"]).path)
            return Path(ln.Artifact.get(key=row["qptiff_key"]).path)
        except (ln.Artifact.DoesNotExist, ln.errors.MultipleResultsFound) as e:
            named = row.get("qptiff_uid") or row.get("qptiff_key")
            print(f"  WARN: cannot resolve {named!r}: {type(e).__name__}")
            return None

    pairs: list[tuple[Path, list[dict]]] = []
    n_kept = n_dropped = 0
    for row in rows:
        path = _qptiff_path(row)
        if path is None:
            continue
        kept = [d for d in row["detections"] if d["confidence"] >= min_conf]
        n_kept += len(kept)
        n_dropped += len(row["detections"]) - len(kept)
        pairs.append((path, kept))
    if min_conf > 0:
        print(f"conf >= {min_conf}: kept {n_kept}, dropped {n_dropped} detection(s)")

    if per_image:
        print(f"Rendering {len(pairs)} per-image JPEGs to {out_dir}/")
        for path, detections in pairs:
            if skip_empty and not detections:
                continue
            try:
                render_overlay(
                    path, detections, out_dir / f"{path.stem}.jpg",
                    max_size=max_size, jpeg_quality=jpeg_quality,
                    title=f"{path.stem[:64]}  ({len(detections)} det)",
                )
            except Exception as e:
                print(f"  ERROR {path.name}: {type(e).__name__}: {str(e)[:120]}")

    if contact_sheets:
        sheets = render_contact_sheets(
            pairs, out_dir, per_sheet=per_sheet, cols=cols, cell_px=cell_px)
        print(f"Wrote {len(sheets)} contact sheet(s) to {out_dir}/")
    return out_dir
