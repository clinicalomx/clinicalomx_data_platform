"""The tma_core stage configs: one model per yaml in ``configs/``. Imports pydantic only."""
from __future__ import annotations

from croi.config import (
    ArtifactQuery,
    ConfigModel,
    KeyedOutput,
    ModelSelector,
    Output,
    Ref,
    SplitsOutput,
    SplitStageParameters,
    StageConfig,
)


class IngestLabelsInputs(ConfigModel):
    images: Ref
    notes: Ref


class IngestLabelsOutputs(ConfigModel):
    labels: Output
    dataset: KeyedOutput


class IngestLabelsConfig(StageConfig):
    """``ingest-labels``: register Label Studio core labels against their source images."""

    data_dir: str
    inputs: IngestLabelsInputs
    outputs: IngestLabelsOutputs


class SplitInputs(ConfigModel):
    dataset: Ref
    categories: Ref


class SplitOutputs(ConfigModel):
    splits: SplitsOutput


class SplitConfig(StageConfig):
    """``split``: deterministic, slide-stratified train/val/test Collections."""

    inputs: SplitInputs
    outputs: SplitOutputs
    parameters: SplitStageParameters


class ExtractTmaCoresInputs(ConfigModel):
    qptiffs: ArtifactQuery
    general_model: ModelSelector
    core_model: ModelSelector


class ExtractTmaCoresOutputs(ConfigModel):
    tma_shapes: Output
    core_shapes: Output


class ExtractTmaCoresParameters(ConfigModel):
    tma_conf: float | None = None
    core_conf: float | None = None
    #: ``[n_rows, n_cols]``, a prior on the grid; None infers it from the detections.
    expected_shape: list[int] | None = None


class ExtractTmaCoresConfig(StageConfig):
    """``extract-tma-cores``: dearray each QPTIFF into TMA and Core shapes and the ROI graph."""

    inputs: ExtractTmaCoresInputs
    outputs: ExtractTmaCoresOutputs
    parameters: ExtractTmaCoresParameters = ExtractTmaCoresParameters()
    limit: int | None = None
    #: One qptiff Artifact key to dearray instead of the query, as ``--image`` sets it.
    image: str | None = None
