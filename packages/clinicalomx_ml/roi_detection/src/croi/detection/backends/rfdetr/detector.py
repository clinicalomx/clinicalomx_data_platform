"""An RF-DETR checkpoint as an `ObjectDetector`, optionally with multi-scale fusion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from croi.detection.detector import (
    DetectedObject,
    ResolveAlias,
    fuse,
    resolve_model_path,
    suppress_contained,
)


class Detector:
    """A single RF-DETR checkpoint, optionally with multi-scale fusion.

    A checkpoint does not record its training resolution, so ``imgsz`` always comes from the
    caller (default 640). One model is built per resolution, on first use. ``tta`` raises.
    ``extra`` goes to ``RFDETR.predict``.
    """

    DEFAULT_WEIGHTS: str | None = None

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
        if tta:
            raise ValueError("the rfdetr backend has no test-time augmentation; drop `tta`")
        self.weights = str(resolve_model_path(weights, resolve_alias))
        self.imgsz_list = [imgsz] if isinstance(imgsz, int) else list(imgsz)
        self.fuse_iou = fuse_iou
        self.contained_thr = contained_thr
        self.contained_agnostic = contained_agnostic
        self.extra = extra or {}
        self._models: dict[int, Any] = {}
        self.model = self._model_at(self.imgsz_list[0])

    def _model_at(self, imgsz: int) -> Any:
        import rfdetr

        if imgsz not in self._models:
            self._models[imgsz] = rfdetr.from_checkpoint(self.weights, resolution=int(imgsz))
        return self._models[imgsz]

    @property
    def names(self) -> dict[int, str]:
        return dict(enumerate(self.model.class_names))

    def predict(self, image: Any, conf: float = 0.25, imgsz: int | None = None) -> list[DetectedObject]:
        if isinstance(image, Path):
            image = str(image)
        elif not isinstance(image, str) and not hasattr(image, "getbands"):
            # BGR array per the contract; RF-DETR reads RGB. Contiguous: negative strides
            # cannot become a tensor.
            image = np.ascontiguousarray(np.asarray(image)[..., ::-1])
        scales = [imgsz] if imgsz is not None else self.imgsz_list
        flat = [
            d for s in scales
            for d in _rfdetr_detections(
                self._model_at(s).predict(image, threshold=conf, **self.extra), self.names)
        ]
        fused = fuse(flat, iou_thr=self.fuse_iou) if len(scales) > 1 else flat
        return suppress_contained(fused, self.contained_thr, self.contained_agnostic)


def _rfdetr_detections(result: Any, names: dict[int, str]) -> list[DetectedObject]:
    """Convert an RF-DETR ``supervision.Detections`` to a DetectedObject list.

    Class ids index the checkpoint's ``class_names``. An id past ``class_names`` is dropped.
    """
    return [
        DetectedObject(
            class_id=int(cls),
            class_name=names[int(cls)],
            confidence=float(score),
            xyxy=tuple(float(v) for v in box),
        )
        for box, score, cls in zip(result.xyxy, result.confidence, result.class_id)
        if int(cls) in names
    ]
