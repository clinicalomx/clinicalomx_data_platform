"""Several detectors as one: their predictions fused into a single set."""

from __future__ import annotations

from typing import Any

from croi.detection.detector import DetectedObject, ObjectDetector, fuse, suppress_contained


class Detector:
    """Several detectors, possibly of different frameworks, with predictions fused into one set."""

    def __init__(
        self,
        members: list[ObjectDetector],
        *,
        fuse_iou: float = 0.6,
        contained_thr: float = 0.9,
        contained_agnostic: bool = False,
    ) -> None:
        if not members:
            raise ValueError("ensemble.Detector needs at least one member")
        self.members = members
        self.fuse_iou = fuse_iou
        self.contained_thr = contained_thr
        self.contained_agnostic = contained_agnostic

    @property
    def names(self) -> dict[int, str]:
        return self.members[0].names

    def predict(self, image: Any, conf: float = 0.25, imgsz: int | None = None) -> list[DetectedObject]:
        flat = [d for m in self.members for d in m.predict(image, conf=conf, imgsz=imgsz)]
        fused = fuse(flat, iou_thr=self.fuse_iou)
        return suppress_contained(fused, self.contained_thr, self.contained_agnostic)
