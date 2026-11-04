"""Log a trained detector checkpoint as an MLflow model.

Wraps the checkpoint as a pyfunc over `ObjectDetector`, so each training run produces a
LoggedModel and a registered-model version (``roi-general`` / ``roi-core``) for any framework.
The checkpoint is always logged as ``best.pt``: ``clinicalomx.models`` finds it by globbing
``*.pt``. The ``model_framework`` tag records which framework reads it. Versions logged under
older module or class names do not load through ``mlflow.pyfunc.load_model``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import mlflow

from croi import feature_names


class MlflowDetectorModel(mlflow.pyfunc.PythonModel):
    """pyfunc wrapper so a checkpoint of either framework is a loadable MLflow model."""

    def __init__(self, framework: str) -> None:
        self.framework = framework

    def load_context(self, context):
        from croi.detection.backends import detector_for

        self._detector = detector_for({
            "framework": self.framework, "weights": context.artifacts["weights"],
        })

    def predict(self, context, model_input, params=None):
        # model_input: one image the ObjectDetector accepts.
        return self._detector.predict(model_input, **(params or {}))


def log_detector_model(
    checkpoint, registered_model_name: str, *, framework: str,
    pip_requirements: list[str], name: str = "model",
):
    """Log ``checkpoint`` as a pyfunc model, a LoggedModel and a registry version. Returns ``ModelInfo``.

    Call inside an active MLflow run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "best.pt"
        shutil.copy2(checkpoint, staged)
        info = mlflow.pyfunc.log_model(
            name=name,
            python_model=MlflowDetectorModel(framework),
            artifacts={"weights": str(staged)},
            registered_model_name=registered_model_name,
            pip_requirements=pip_requirements,
        )
    return info


def stamp_mlflow_features(
    artifact,
    *,
    registered_model: str,
    model_version: str | None,
    run_id: str,
    run_name: str | None = None,
    experiment_name: str | None = None,
) -> None:
    """Stamp MLflow coordinates on a lamindb model artifact so it is fetchable off-cluster.

    Writes ``mlflow_run_id``, ``mlflow_run_name``, ``mlflow_experiment_name``,
    ``mlflow_artifact_uri`` and, when ``model_version`` is set, ``model_uri``. Raises on any failure.
    Uses plain get-or-create features: ``save_mlflow_features()`` creates duplicates on an
    existing instance.
    """
    import lamindb as ln

    vals: dict[str, str] = {
        feature_names.MLFLOW_RUN_ID: run_id,
        feature_names.MLFLOW_ARTIFACT_URI: mlflow.get_run(run_id).info.artifact_uri,
    }
    if model_version is not None:
        vals[feature_names.MODEL_URI] = f"models:/{registered_model}/{model_version}"
    if run_name:
        vals[feature_names.MLFLOW_RUN_NAME] = run_name
    if experiment_name:
        vals[feature_names.MLFLOW_EXPERIMENT_NAME] = experiment_name

    for name in vals:
        if not ln.Feature.filter(name=name).exists():
            ln.Feature(name=name, dtype="str").save()
    # add, never set: `set_values` replaces every external value, `model_framework` included.
    artifact.features.add_values(vals)
