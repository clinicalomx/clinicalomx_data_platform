"""Train a detector for any objective: fine-tune from a registered base, register the champion.

The ``train`` CLI stage and the train flows call :func:`run`. Steps take the objective by name,
since lamindb records step arguments as JSON.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import lamindb as ln
import mlflow
from clinicalomx import InputRef

from croi import feature_names, objectives
from croi.detection.backends.training import trainer_for
from croi.detection.packaging import log_detector_model, stamp_mlflow_features
from croi.detection.tracking import log_collection_inputs
from croi.detection.trainer import Trainer
from croi.environment import StageEnvironment
from croi.evaluation.evaluate import evaluate_model


def ensure_base_model(
    trainer: Trainer,
    data_dir: Path,
    data_dir_rel: str,
    output_storage: ln.Storage,
) -> tuple[ln.Artifact, Path]:
    """Fetch the trainer's base checkpoint into {data_dir}/base_models and register it.

    Returns ``(artifact, local_path)``.
    """
    base_models_dir = data_dir / "base_models"
    base_models_dir.mkdir(parents=True, exist_ok=True)
    base_pt = trainer.fetch_base(base_models_dir)
    key = f"{data_dir_rel}/base_models/{base_pt.name}"
    art = InputRef(kind="artifact", key=key).ensure(base_pt, storage=output_storage)
    return art, base_pt


def stamp_framework(artifact: ln.Artifact, framework: str) -> None:
    """Record the framework that reads ``artifact``'s checkpoint, for ``framework_of``."""
    if not ln.Feature.filter(name=feature_names.MODEL_FRAMEWORK).exists():
        ln.Feature(name=feature_names.MODEL_FRAMEWORK, dtype="str").save()
    # add, never set: `set_values` replaces every external value already on the artifact.
    artifact.features.add_values({feature_names.MODEL_FRAMEWORK: framework})


# Pinned uid: a source change versions this file's Transform instead of forking it.
@ln.step(uid="wl8ndVgVLIMs")
def train_model(objective: str, config: dict, output_storage: ln.Storage):
    """Materialize the split, fine-tune from a registered base, register the champion.

    Returns ``(weights Artifact or None, failure or None)``. ``failure`` is an MLflow error after
    training; the champion is registered regardless, and the caller raises it after evaluating.
    """
    record = objectives.get(objective)
    registered_model = record.registry.registered_model
    params = config["parameters"]
    model_output = config["outputs"]["model"]
    name = params["name"]
    data_dir_rel = config["data_dir"]
    splits_prefix = config["inputs"]["train_split"]["key"].rsplit("/", 1)[0]

    data_dir = output_storage.path / data_dir_rel
    dataset = record.load_dataset(config)
    for finding in dataset.validate():
        print(f"  dataset: {finding}")
    coco = dataset.to_coco()  # refuses a malformed dataset before training
    print(f"  dataset: {dataset.describe()}")

    experiment = params["mlflow_experiment_name"]
    mlflow.set_experiment(experiment)
    os.environ["MLFLOW_EXPERIMENT_NAME"] = experiment
    print(f"MLflow train experiment: {experiment}")

    trainer = trainer_for(params)
    print(f"  framework: {trainer.framework}")
    base_model_art, base_pt = ensure_base_model(trainer, data_dir, data_dir_rel, output_storage)

    result = trainer.fit(coco, data_dir / "runs" / name, base_pt)
    if result is None:
        return None, None
    save_dir, best_pt, resumed_from = result.save_dir, result.best, result.resumed_from

    # From here an MLflow error is collected; the champion is registered anyway.
    failures: list[Exception] = []
    mlflow_run_id = mlflow_run_name = mlflow_version = None
    last = mlflow.last_active_run()
    if last is None:
        failures.append(RuntimeError(
            f"training left no MLflow run, so the {registered_model} champion has no MLflow "
            "version or coordinates"))
    else:
        mlflow_run_id, mlflow_run_name = last.info.run_id, last.info.run_name
        try:
            with mlflow.start_run(run_id=mlflow_run_id):
                if resumed_from:
                    mlflow.set_tag("resumed_from", resumed_from)
                log_collection_inputs(dataset.collections or {})
                mlflow.log_artifacts(str(save_dir), artifact_path="run")
                for k, v in result.test_metrics.items():
                    mlflow.log_metric(f"test/{k}", v)
                mlflow.set_tag(feature_names.MODEL_FRAMEWORK, trainer.framework)
                # Registers a roi-<obj> version from this run; promote() sets aliases on it.
                log_detector_model(best_pt, registered_model, framework=trainer.framework,
                                   pip_requirements=trainer.pip_requirements, name=registered_model)
        except Exception as e:
            failures.append(e)
        try:
            from mlflow.tracking import MlflowClient
            for mv in MlflowClient().search_model_versions(f"name='{registered_model}'"):
                if mv.run_id == mlflow_run_id:
                    mlflow_version = mv.version
                    break
        except Exception as e:
            failures.append(e)
        else:
            if mlflow_version is None and not failures:
                failures.append(RuntimeError(
                    f"{registered_model} has no version from MLflow run {mlflow_run_id}"))

    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    staged_pt = models_dir / "best.pt"  # the platform's checkpoint name for every framework
    shutil.copy2(best_pt, staged_pt)

    # Key: <registered_model>_v<MLflow version>_<arch>.pt, one Artifact per version.
    # The MLflow alias, not lamindb is_latest, says which is production.
    arch = trainer.arch
    vtag = f"v{mlflow_version}" if mlflow_version else save_dir.name
    base_dir = str(Path(model_output["key"]).parent)
    model_key = f"{base_dir}/{registered_model}_{vtag}_{arch}.pt"
    storage_root = Path(str(output_storage.path))
    governed_pt = storage_root / model_key
    governed_pt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staged_pt, governed_pt)

    weights_art = ln.Artifact(
        governed_pt, key=model_key, kind="model", storage=output_storage,
        version=mlflow_version,
        description=f"{model_output['description']} ({registered_model} {vtag} {arch}, "
                    f"trained on {splits_prefix}, run={save_dir.name}).",
    ).save()
    stamp_framework(weights_art, trainer.framework)
    print(f"  registered champion {weights_art.key} (uid={weights_art.uid}, "
          f"{trainer.framework}, mlflow v{mlflow_version})")

    for art in dataset.artifacts():
        weights_art.run.input_artifacts.add(art)
    if base_model_art is not None:
        weights_art.run.input_artifacts.add(base_model_art)

    if mlflow_run_id:
        try:
            stamp_mlflow_features(
                weights_art, registered_model=registered_model, model_version=mlflow_version,
                run_id=mlflow_run_id,
                run_name=mlflow_run_name, experiment_name=experiment,
            )
        except Exception as e:
            failures.append(e)

    failure = None
    if failures:
        *earlier, failure = failures
        for e in earlier:
            failure.add_note(f"Also: {e}")
    return weights_art, failure


@ln.flow(uid="wl8ndVgVLIMs")
def train_and_eval(
    objective: str,
    config: dict,
    project: ln.Project,
    output_storage: ln.Storage,
    eval_only: bool = False,
    weights_selector: str | None = None,
):
    """The train stage as one lamindb flow: train then evaluate, or evaluate a champion."""

    if eval_only:
        # Re-evaluate an existing champion; a full run always retrains.
        weights_art = objectives.get(objective).registry.resolve_champion(weights_selector)
        print(f"Eval-only: {weights_art.key} v{weights_art.version} (uid={weights_art.uid})")
        evaluate_model(objective, config, weights_art, output_storage)
        print("\nDone (eval-only).")
        return

    weights_art, failure = train_model(objective, config, output_storage)
    if weights_art is not None:
        try:
            evaluate_model(objective, config, weights_art, output_storage)
        except Exception as e:
            if failure is not None:
                e.add_note(f"Also, before evaluation: {failure}")
            raise
    else:
        print("Skipping eval: no champion produced.")
    if failure is not None:
        raise failure

    print("\nDone (train + eval).")


def run(objective: str, stage: StageEnvironment, config: dict, *, eval_only: bool = False,
        weights: str | None = None) -> None:
    """The CLI's train stage: set the MLflow tracking URI, then run the flow."""
    uri = stage.manifest.mlflow_uri()
    mlflow.set_tracking_uri(uri)
    print(f"MLflow tracking: {uri}")
    train_and_eval(
        objective=objective, config=config, project=stage.project,
        output_storage=stage.storage, eval_only=eval_only, weights_selector=weights,
    )
