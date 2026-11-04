"""A real, decodable PNG of a given size, so a test's images have the size a reader measures."""
from __future__ import annotations

import struct
import zlib


def png_bytes(width: int, height: int) -> bytes:
    """An 8-bit grey PNG of ``width`` x ``height``, every pixel black."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))
