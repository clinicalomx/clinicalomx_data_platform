"""Read stored things: a config input's record, a PNG's size, a Label Studio YOLO label's rows.

:class:`ImageSource` is the only part needing a database.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ImageSource(Protocol):
    """How a config ``inputs`` block becomes records. The only part needing a database."""

    def get(self, ref: Mapping[str, Any]) -> Any:
        """Resolve one ``InputRef``-shaped mapping to its lamindb record."""


class _LaminImageSource:
    """The default source: `clinicalomx.InputRef`, imported lazily."""

    def get(self, ref: Mapping[str, Any]) -> Any:
        from clinicalomx import InputRef

        return InputRef(**dict(ref)).get()


def default_image_source() -> ImageSource:
    return _LaminImageSource()


def classes_in_label(label_path: str | Path) -> frozenset[int]:
    """The class indices a YOLO label file names. A malformed row contributes nothing."""
    out = set()
    for line in Path(label_path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            out.add(int(parts[0]))
        except ValueError:
            continue
    return frozenset(out)


def label_rows(label_path: str | Path) -> list[tuple[int, float, float, float, float]]:
    """Every ``class cx cy w h`` row of a label file, in order. Raises on a malformed row."""
    rows = []
    for n, line in enumerate(Path(label_path).read_text().splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        try:
            if len(parts) != 5:
                raise ValueError(f"{len(parts)} fields")
            rows.append((int(parts[0]), *(float(v) for v in parts[1:])))
        except ValueError as e:
            raise ValueError(f"line {n} is not `class cx cy w h` ({e})") from None
    return rows


def png_size(path: str | Path) -> tuple[int, int]:
    """``(width, height)`` from a PNG header, without decoding."""
    # PNG only, the one suffix every objective's `pairs_in` pairs.
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])
