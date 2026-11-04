"""Stage configs as typed models, so a misspelt key fails when the yaml loads.

Every stage yaml has ``data_dir``, ``inputs``, ``outputs`` and ``parameters``. A stage subclasses
:class:`StageConfig`, and :func:`load` validates the file before any stage code runs. Models are
strict, frozen and forbid unknown keys. Stages still receive the plain mapping. ``None`` means the
stage applies its own default. ``train:`` blocks, ``InputRef`` mappings and ``ArtifactQuery``
filters pass through unvalidated. Imports only pydantic and yaml.
"""
from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, NamedTuple

import pydantic as pdt
import yaml

#: Every framework a stage config may name: the keys of ``DETECTORS`` and ``TRAINERS``.
Framework = Literal["ultralytics", "rfdetr"]

#: The framework a config without ``framework:`` means. Registered models never default.
DEFAULT_FRAMEWORK: Framework = "ultralytics"

#: An ``InputRef`` mapping (``{kind, key, uid, version, source}``), validated by ``clinicalomx.InputRef``.
Ref = dict[str, Any]


class InvalidConfig(ValueError):
    """A stage yaml does not match the model of the stage that reads it."""


class ConfigModel(pdt.BaseModel, strict=True, frozen=True, extra="forbid"):
    """Strict, frozen, refuses unknown keys: the base of every config section."""


class StageConfig(ConfigModel):
    """The four-section grammar every stage yaml shares.

    ``data_dir`` is the staging directory under the storage root. A stage without
    ``parameters:`` keeps an empty one that refuses every key.
    """

    data_dir: str | None = None
    inputs: ConfigModel
    outputs: ConfigModel
    parameters: ConfigModel = ConfigModel()




class Output(ConfigModel):
    """One produced thing: its stable key, a per-item key template or prefix, a description."""

    key: str | None = None
    key_template: str | None = None
    key_prefix: str | None = None
    description: str | None = None


class KeyedOutput(ConfigModel):
    """A produced Collection or Artifact the stage registers under one key with a description."""

    key: str
    description: str


class ArtifactQuery(ConfigModel, extra="allow"):
    """A discovered input: every latest Artifact matching a filter.

    Extra keys go to ``ln.Artifact.filter``, which refuses unknown fields when the stage runs.
    """

    query: Literal["artifacts"]
    suffix: str | None = None
    exclude_key_contains: list[str] | None = None


class ModelSelector(ConfigModel):
    """Which registered champion: a lamindb or MLflow coordinate. Empty means lamindb ``production``.

    ``run_id`` and ``version`` are older spellings of the MLflow keys.
    """

    uid: str | None = None
    key: str | None = None
    alias: str | None = None
    mlflow_version: str | int | None = None
    mlflow_alias: str | None = None
    mlflow_run_id: str | None = None
    run_id: str | None = None
    version: str | int | None = None


class DetectorConfig(ConfigModel):
    """A ``model:`` block, as ``croi.detection.backends.detector_for`` reads it."""

    framework: Framework | None = None
    weights: str | dict[str, Any] | None = None
    tta: bool | None = None
    imgsz: int | list[int] | None = None
    fuse_iou: float | None = None
    contained_thr: float | None = None
    contained_agnostic: bool | None = None
    ensemble: list[str | dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None




class SplitParameters(ConfigModel):
    """The deterministic split. Changing any field reshuffles every split."""

    validation_fraction: float
    test_fraction: float
    seed: int


class SplitStageParameters(ConfigModel):
    split: SplitParameters


class SplitsOutput(ConfigModel):
    """The three split Collections, keyed ``<key_prefix>/{train,val,test}``."""

    key_prefix: str
    description: str




class EvalParameters(ConfigModel):
    """How ``croi.evaluation.evaluate`` scores the champion a train run registers."""

    mlflow_experiment: str
    imgsz: int
    conf: float
    worst_n: int
    split: Literal["train", "val", "test"] | None = None
    co_occurrence: bool | None = None


class TrainParameters(ConfigModel):
    """What the train stage reads, for any framework. ``train`` passes verbatim to the framework."""

    name: str
    mlflow_experiment_name: str
    resume: Literal["auto"] | bool | None = None
    train: dict[str, Any]
    eval: EvalParameters
    #: Read by ``DetectionDataset.from_config`` into its report.
    release_version: str | None = None


class UltralyticsTrainParameters(TrainParameters):
    """``croi.detection.backends.ultralytics.trainer.Trainer``'s parameters."""

    framework: Literal["ultralytics"] | None = None
    ultralytics_model_name: str | None = None
    mlflow_log_period: int | None = None


class RfdetrTrainParameters(TrainParameters):
    """``croi.detection.backends.rfdetr.trainer.Trainer``'s parameters."""

    framework: Literal["rfdetr"]
    rfdetr_model_name: str | None = None


def _framework_of(params: Any) -> str:
    """The framework a ``parameters:`` block trains; absent means the default."""
    if isinstance(params, Mapping):
        return params.get("framework") or DEFAULT_FRAMEWORK
    return params.framework or DEFAULT_FRAMEWORK


#: One parameter model per framework, chosen by ``framework:``.
AnyTrainParameters = Annotated[
    Annotated[UltralyticsTrainParameters, pdt.Tag("ultralytics")]
    | Annotated[RfdetrTrainParameters, pdt.Tag("rfdetr")],
    pdt.Discriminator(_framework_of),
]


class TrainInputs(ConfigModel):
    train_split: Ref
    val_split: Ref
    test_split: Ref
    categories: Ref


class TrainOutputs(ConfigModel):
    model: KeyedOutput
    metrics: KeyedOutput
    contact_sheet: KeyedOutput


class TrainConfig(StageConfig):
    """The train stage (``croi.train``) and the scorer it calls, for any objective."""

    data_dir: str
    inputs: TrainInputs
    outputs: TrainOutputs
    parameters: AnyTrainParameters




def deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` into ``base``: dicts merge, everything else replaces."""
    out = dict(base)
    for k, v in over.items():
        out[k] = (
            deep_merge(out[k], v)
            if isinstance(v, dict) and isinstance(out.get(k), dict)
            else v
        )
    return out


def validate(schema: type[StageConfig], config: Mapping[str, Any], *, source: object) -> StageConfig:
    """``config`` as ``schema``, or :class:`InvalidConfig` naming ``source`` and every bad key."""
    try:
        return schema.model_validate(config)
    except pdt.ValidationError as e:
        raise InvalidConfig(f"{source} is not a valid {schema.__name__}:\n{e}") from e


def load(path: str | os.PathLike, schema: type[StageConfig],
         overrides: dict | None = None) -> dict:
    """Read a stage yaml, deep-merge ``overrides``, validate against ``schema``. Returns the mapping."""
    with open(path) as f:
        config = yaml.safe_load(f)
    if overrides:
        config = deep_merge(config, overrides)
    validate(schema, config, source=path)
    return config




class Subcommand(NamedTuple):
    """One stage of ``python -m croi <objective> <stage>``.

    ``config`` and ``schema`` are None for a stage with no config. ``arguments`` adds CLI options.
    """

    config: str | None
    schema: type[StageConfig] | None
    run: Callable[[argparse.Namespace], None]
    help: str
    arguments: Callable[[argparse.ArgumentParser], None] | None = None
