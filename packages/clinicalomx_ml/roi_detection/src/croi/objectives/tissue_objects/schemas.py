"""The general objective's dataset report, over the shared models in ``croi.schemas``."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from croi.schemas import SplitCounts, ValidationReport


class DatasetSummary(BaseModel):
    """A ``DetectionDataset`` reduced to what a report or a Prefect artifact shows."""

    model_config = ConfigDict(extra="forbid")

    name: str
    objective: Literal["general"] = "general"
    categories: list[str]
    counts: SplitCounts
    release_version: str | None = None
    validation: ValidationReport = Field(default_factory=ValidationReport)

    @classmethod
    def of(cls, dataset, *, validate: bool = True) -> DatasetSummary:
        """Summarise a `TissueObjectsDataset`. ``validate=False`` skips reading every label file."""
        return cls(
            name=dataset.name,
            categories=list(dataset.categories),
            counts=SplitCounts(**dict(dataset.counts())),
            release_version=dataset.release_version,
            validation=ValidationReport(findings=dataset.validate() if validate else []),
        )
