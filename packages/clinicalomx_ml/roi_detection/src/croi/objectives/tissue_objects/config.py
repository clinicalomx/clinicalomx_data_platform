"""The tissue_objects stage configs: one model per yaml in ``configs/``. Imports pydantic only."""
from __future__ import annotations

from typing import Literal

from croi.config import (
    ArtifactQuery,
    ConfigModel,
    DetectorConfig,
    KeyedOutput,
    ModelSelector,
    Output,
    Ref,
    SplitsOutput,
    SplitStageParameters,
    StageConfig,
)


class IngestInputs(ConfigModel):
    qptiffs: ArtifactQuery


class IngestOutputs(ConfigModel):
    thumbnails: Output


class IngestParameters(ConfigModel):
    force: bool | None = None


class IngestConfig(StageConfig):
    """``ingest``: QPTIFF to thumbnail PNG and a WholeSlide ROI."""

    inputs: IngestInputs
    outputs: IngestOutputs
    parameters: IngestParameters = IngestParameters()
    limit: int | None = None


class IngestLabelsInputs(ConfigModel):
    thumbnails: Ref
    notes: Ref


class IngestLabelsOutputs(ConfigModel):
    labels: KeyedOutput


class LabelImport(ConfigModel):
    """Where this round's labels come from: a Label Studio JSON export, or label files on disk."""

    tasks_json: str | None = None


class IngestLabelsParameters(ConfigModel):
    label_import: LabelImport


class IngestLabelsConfig(StageConfig):
    """``ingest-labels``: register Label Studio labels against their thumbnails."""

    data_dir: str
    inputs: IngestLabelsInputs
    outputs: IngestLabelsOutputs
    parameters: IngestLabelsParameters


class SplitInputs(ConfigModel):
    thumbnails: Ref
    labels: Ref
    categories: Ref


class SplitOutputs(ConfigModel):
    splits: SplitsOutput


class SplitConfig(StageConfig):
    """``split``: deterministic train/val/test Collections."""

    inputs: SplitInputs
    outputs: SplitOutputs
    parameters: SplitStageParameters


class ExtractTmaCropsInputs(ConfigModel):
    thumbnails: Ref
    model: ModelSelector


class ExtractTmaCropsOutputs(ConfigModel):
    crops: Output
    release: KeyedOutput


class ExtractTmaCropsParameters(ConfigModel):
    target_class: str | None = None
    conf: float | None = None
    min_conf: float | None = None
    imgsz: int | None = None
    #: None versions the release by the model that cut it.
    release_version: str | None = None
    backend: DetectorConfig | None = None


class ExtractTmaCropsConfig(StageConfig):
    """``extract-tma-crops``: detect TMA grids and cut the crop release tma_core trains on."""

    inputs: ExtractTmaCropsInputs
    outputs: ExtractTmaCropsOutputs
    parameters: ExtractTmaCropsParameters = ExtractTmaCropsParameters()
    limit: int | None = None


class ExtractPyramidInputs(ConfigModel):
    qptiffs: ArtifactQuery


class ExtractPyramidOutputs(ConfigModel):
    projections: Output
    release: KeyedOutput


class ExtractPyramidParameters(ConfigModel):
    target_max_edge: int | None = None
    mode: Literal["max", "mean", "auto"] | None = None
    release_version: str | None = None


class ExtractPyramidConfig(StageConfig):
    """``extract-pyramid``: the low-res pyramid projection release."""

    inputs: ExtractPyramidInputs
    outputs: ExtractPyramidOutputs
    parameters: ExtractPyramidParameters = ExtractPyramidParameters()
    limit: int | None = None
