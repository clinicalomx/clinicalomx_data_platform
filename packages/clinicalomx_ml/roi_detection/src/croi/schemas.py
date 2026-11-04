"""Pydantic models for a detection request and response, and the dataset report.

No server binds to these. Each objective's ``schemas.DatasetSummary`` builds on the report models.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from croi.detection.detector import DetectedObject

__all__ = [
    "DetectedObject",
    "DetectionRequest",
    "DetectionResponse",
    "SplitCounts",
    "ValidationReport",
]


class DetectionRequest(BaseModel):
    """One detection ask: which image, which champion, at what thresholds."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    image: str = Field(description="Path or lamindb Artifact uid of the image to detect on.")
    alias: str = Field(default="production", description="Model alias to resolve the champion.")
    conf: float = Field(default=0.25, ge=0.0, le=1.0)
    imgsz: int | None = Field(default=None, description="None means the backend's own default.")


class DetectionResponse(BaseModel):
    """The detection result and the champion that produced it."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    image: str
    weights: str = Field(description="Resolved weights path or model uid.")
    detections: list[DetectedObject] = Field(default_factory=list)


class SplitCounts(BaseModel):
    """Pairs per split, plus the images no split will train on."""

    model_config = ConfigDict(extra="forbid")

    train: int = 0
    val: int = 0
    test: int = 0
    unlabelled: int = 0


class ValidationReport(BaseModel):
    """What ``DetectionDataset.validate`` found. ``ok`` is derived from the findings."""

    model_config = ConfigDict(extra="forbid")

    findings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings
