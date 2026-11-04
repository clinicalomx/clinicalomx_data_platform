"""The inference contract: what a detector returns, and the overlap guarantees on its boxes.

Callers code against `DetectedObject` and `ObjectDetector`, never a framework.
`backends.detector_for` builds one from a `model:` config block. This module imports no framework.
Images follow the Ultralytics contract: a path, a PIL image, or an HWC BGR array.
Checkpoints use a `.pt` key for every framework; the framework is recorded on the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import lamindb as ln

#: An objective's alias resolver, such as ``croi.objectives.<obj>.registry.resolve_weights``.
ResolveAlias = Callable[[str], Path]


@dataclass
class DetectedObject:
    """One predicted box in image-pixel coordinates."""

    class_id: int
    class_name: str
    confidence: float
    xyxy: tuple[float, float, float, float]


@runtime_checkable
class ObjectDetector(Protocol):
    """Anything that maps an image to a list of `DetectedObject`s.

    `names` maps class id to class name, as ultralytics' `model.names` does.
    """

    names: dict[int, str]

    def predict(self, image: Any, conf: float, imgsz: int) -> list[DetectedObject]:
        ...


def resolve_model_path(spec: str | Path | dict, resolve_alias: ResolveAlias | None = None) -> Path:
    """Resolve a weights spec to a local .pt path.

    Accepts:
      - a filesystem path (str/Path) to a .pt file
      - a bare alias string (e.g. "production"), resolved by ``resolve_alias``
      - {"alias": "..."}            -> registry alias
      - {"lamin_uid": "..."}        -> lamindb Artifact by uid
      - {"lamin_key": "..."}        -> lamindb Artifact by key
      - {"path": "..."}             -> explicit path

    An alias without ``resolve_alias`` raises.
    """
    if isinstance(spec, dict):
        if "path" in spec:
            return Path(spec["path"])
        if "alias" in spec:
            return _resolve_alias(spec["alias"], resolve_alias)
        if "lamin_uid" in spec:
            return Path(ln.Artifact.get(uid=spec["lamin_uid"]).cache())
        if "lamin_key" in spec:
            return Path(ln.Artifact.get(key=spec["lamin_key"]).cache())
        raise ValueError(f"unrecognized weights spec dict: {spec!r}")

    p = Path(spec)
    # A .pt suffix or an existing path is a path; anything else is an alias.
    if p.suffix == ".pt" or p.exists():
        return p
    return _resolve_alias(str(spec), resolve_alias)


def _resolve_alias(alias: str, resolve_alias: ResolveAlias | None) -> Path:
    if resolve_alias is None:
        raise ValueError(
            f"weights {alias!r} name a registry alias, and no objective registry was given to "
            "resolve it; pass `resolve_alias=croi.objectives.<obj>.registry.resolve_weights`"
        )
    return resolve_alias(alias)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _ios(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection over the smaller box's area. Near 1.0 when one box sits inside the other."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    smaller = min(area_a, area_b)
    return inter / smaller if smaller > 0 else 0.0


def suppress_contained(
    dets: list[DetectedObject], ios_thr: float = 0.9, class_agnostic: bool = False
) -> list[DetectedObject]:
    """Drop a box whose intersection over its own area with a higher-confidence box exceeds ``ios_thr``.

    Same-class unless ``class_agnostic``.
    """
    if not ios_thr:
        return dets
    kept: list[DetectedObject] = []
    for det in sorted(dets, key=lambda d: d.confidence, reverse=True):
        if any(
            (class_agnostic or det.class_id == k.class_id) and _ios(det.xyxy, k.xyxy) >= ios_thr
            for k in kept
        ):
            continue
        kept.append(det)
    return kept


def fuse(
    dets: list[DetectedObject], iou_thr: float = 0.6, class_agnostic: bool = False
) -> list[DetectedObject]:
    """Greedy NMS across passes or models: the highest-confidence box wins at IoU >= ``iou_thr``.

    Same-class unless ``class_agnostic``.
    """
    kept: list[DetectedObject] = []
    for det in sorted(dets, key=lambda d: d.confidence, reverse=True):
        if any(
            (class_agnostic or det.class_id == k.class_id) and _iou(det.xyxy, k.xyxy) >= iou_thr
            for k in kept
        ):
            continue
        kept.append(det)
    return kept


def suppress_overlaps(
    dets: list[DetectedObject],
    *,
    iou_thr: float = 0.6,
    ios_thr: float = 0.9,
    class_agnostic: bool = True,
) -> list[DetectedObject]:
    """Model-agnostic overlap suppression: greedy IoU-NMS, then containment suppression.

    Keeps no two boxes for one object, whatever the detector's head does. Keeps the
    higher-confidence box in each collision. Defaults (iou 0.6, ios 0.9) are deliberately high:
    lower values delete distinct edge objects and break the grid fit in dearray.
    Apply once, to raw boxes: build the backend with ``contained_thr: 0``.
    """
    dets = fuse(dets, iou_thr=iou_thr, class_agnostic=class_agnostic)
    return suppress_contained(dets, ios_thr=ios_thr, class_agnostic=class_agnostic)
