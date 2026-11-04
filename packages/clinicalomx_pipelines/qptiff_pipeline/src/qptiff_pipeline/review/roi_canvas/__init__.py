"""``roi_canvas``: a two-way Streamlit component (fabric.js) for editing ROI boxes.

Right-click a box to change its class. The frontend is plain HTML/JS plus a bundled
``fabric.min.js`` under ``frontend/``; there is no build step.

Usage:
    from qptiff_pipeline.review.roi_canvas import roi_canvas
    result = roi_canvas(
        image_b64=..., boxes=[{"class","xyxy","occupied","rc","bid"}], classes=[...],
        colors={cls: "#rrggbb"}, width=W, height=H, max_display_w=900,
        edges=[{"a": bid, "b": bid, "axis": "row"|"col"}], nonce=ver, key="...",
    )
    # result -> {"boxes": [...], "edges": [...], "nonce": <echo>} or None before first paint

Boxes come back clamped to the image. Bump ``nonce`` to rebuild from ``boxes``; leave it to keep
live edits. Links drawn in Link mode come back in ``edges``, keyed by ``bid``.
"""
from __future__ import annotations

import os
from typing import Any

import streamlit.components.v1 as components

_FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
_component = components.declare_component("roi_canvas", path=_FRONTEND)


def roi_canvas(
    *,
    image_b64: str,
    boxes: list[dict],
    classes: list[str],
    colors: dict[str, str],
    width: float,
    height: float,
    max_display_w: int = 900,
    edges: list[dict] | None = None,
    nonce: Any = 0,
    key: str | None = None,
) -> dict | None:
    """Render the ROI editor. Returns the boxes and links, or None before the first paint."""
    return _component(
        image_b64=image_b64,
        boxes=boxes,
        classes=list(classes),
        colors=colors,
        width=float(width),
        height=float(height),
        max_display_w=int(max_display_w),
        edges=list(edges or []),
        nonce=str(nonce),
        key=key,
        default=None,
    )
