"""The tissue_objects training flows.

``general_train_and_eval`` is the ``train-general`` deployment. ``train_flow`` is the whole DAG
and is not deployed. Imports other than prefect stay inside flow bodies. The flow body runs from
the ``clinical-roi[train]`` wheel in the ``croi-general-train`` image, so a fix here needs a
``clinical-roi-v*`` tag and a new image.
"""
from __future__ import annotations

from prefect import flow, get_run_logger

OBJECTIVE = "tissue_objects"

TRAIN_CONFIG = "3_train_and_eval.yaml"

LABELS_CONFIG = "1_ingest_labels.yaml"
SPLIT_CONFIG = "2_split.yaml"


@flow(name="general-train-and-eval", log_prints=True)
def general_train_and_eval(
    name: str | None = None,
    epochs: int | None = None,
    overrides: dict | None = None,
    eval_only: bool = False,
    weights: str | None = None,
    config: str = TRAIN_CONFIG,
) -> None:
    """Train and evaluate the general ROI detector, or evaluate an existing champion.

    Precedence, low to high: yaml < ``overrides`` < ``name``/``epochs``.

        overrides={"parameters": {"train": {"device": 0}, "eval": {"conf": 0.3}}}

    name: run name, ``parameters.name`` (the resumable run dir and MLflow run label).
    epochs: ``parameters.train.epochs``.
    overrides: dict deep-merged over the yaml config.
    eval_only: skip training and score a registered champion.
    weights: with ``eval_only``, ``uid:<uid>`` or ``version:<n>``; default is the MLflow
        ``production`` alias.
    config: stage yaml name or path; its ``parameters.framework`` picks the trainer.
    """
    logger = get_run_logger()

    from croi.config import TrainConfig, deep_merge
    from croi.objectives.tissue_objects import environment

    if overrides:
        logger.info(f"applied config overrides: {overrides}")
    # Discrete knobs override yaml and overrides, and are validated with them.
    knobs: dict = {}
    if name is not None:
        knobs["name"] = name
    if epochs is not None:
        knobs["train"] = {"epochs": epochs}
    config = environment.load_config(
        config, TrainConfig, deep_merge(overrides or {}, {"parameters": knobs}))
    from croi import train

    logger.info(f"{OBJECTIVE} train_and_eval (eval_only={eval_only}, weights={weights})")
    train.run(OBJECTIVE, environment.preflight(roi_schema=False), config,
              eval_only=eval_only, weights=weights)
    logger.info(f"{OBJECTIVE} train_and_eval done")


@flow(name="general-train-dag", log_prints=True)
def train_flow(
    overrides: dict | None = None, skip_ingest: bool = False, config: str = TRAIN_CONFIG,
) -> list[str]:
    """The whole DAG: ingest_labels -> split -> validate -> train -> evaluate. Not deployed.

    ``skip_ingest`` starts at the split. Each stage opens its own lamindb run.
    Returns the dataset findings from ``validate``.
    """
    logger = get_run_logger()

    from croi.config import TrainConfig
    from croi.objectives.tissue_objects import environment
    from croi.objectives.tissue_objects.config import IngestLabelsConfig, SplitConfig

    # Validate every config before the first stage writes.
    labels_config = None if skip_ingest else environment.load_config(LABELS_CONFIG,
                                                                      IngestLabelsConfig)
    split_config = environment.load_config(SPLIT_CONFIG, SplitConfig)
    config = environment.load_config(config, TrainConfig, overrides)
    from croi import train
    from croi.objectives.tissue_objects import labels, split
    from croi.objectives.tissue_objects.dataset import TissueObjectsDataset

    stage = environment.preflight()

    if labels_config is not None:
        labels.run(labels_config)
    split.run(split_config)

    findings = TissueObjectsDataset.from_config(config).validate()
    for finding in findings:
        logger.warning(f"dataset: {finding}")

    train.run(OBJECTIVE, stage, config)
    return findings
