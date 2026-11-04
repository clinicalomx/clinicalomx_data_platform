"""An ultralytics YOLO checkpoint as an `ObjectDetector`, optionally with TTA and multi-scale fusion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from croi.detection.detector import (
    DetectedObject,
    ResolveAlias,
    fuse,
    resolve_model_path,
    suppress_contained,
)


class Detector:
    """A single YOLO checkpoint, optionally with TTA and multi-scale fusion.

    ``model`` is the ultralytics ``YOLO`` object.
    """

    DEFAULT_WEIGHTS: str | None = "yolo11n.pt"

    def __init__(
        self,
        weights: str | Path | dict,
        *,
        resolve_alias: ResolveAlias | None = None,
        tta: bool = False,
        imgsz: int | list[int] = 640,
        fuse_iou: float = 0.6,
        contained_thr: float = 0.9,
        contained_agnostic: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        from ultralytics import YOLO

        self.model = YOLO(str(resolve_model_path(weights, resolve_alias)))
        self.tta = tta
        self.imgsz_list = [imgsz] if isinstance(imgsz, int) else list(imgsz)
        self.fuse_iou = fuse_iou
        self.contained_thr = contained_thr
        self.contained_agnostic = contained_agnostic
        self.extra = extra or {}

    @property
    def names(self) -> dict[int, str]:
        return self.model.names

    def predict(self, image: Any, conf: float = 0.25, imgsz: int | None = None) -> list[DetectedObject]:
        scales = [imgsz] if imgsz is not None else self.imgsz_list
        passes: list[list[DetectedObject]] = []
        for s in scales:
            result = self.model.predict(
                source=image,
                imgsz=s,
                conf=conf,
                augment=self.tta,
                verbose=False,
                **self.extra,
            )[0]
            passes.append(_ultralytics_detections(result, self.model.names))
        flat = [d for p in passes for d in p]
        fused = fuse(flat, iou_thr=self.fuse_iou) if len(scales) > 1 else flat
        # Drop nested duplicates that IoU-NMS leaves.
        return suppress_contained(fused, self.contained_thr, self.contained_agnostic)


def _ultralytics_detections(result: Any, names: dict[int, str]) -> list[DetectedObject]:
    """Convert an ultralytics Result to a DetectedObject list."""
    out: list[DetectedObject] = []
    for box in result.boxes:
        cls = int(box.cls.item())
        x1, y1, x2, y2 = box.xyxy.cpu().numpy().flatten().tolist()
        out.append(
            DetectedObject(
                class_id=cls,
                class_name=names.get(cls, str(cls)),
                confidence=float(box.conf.item()),
                xyxy=(x1, y1, x2, y2),
            )
        )
    return out
