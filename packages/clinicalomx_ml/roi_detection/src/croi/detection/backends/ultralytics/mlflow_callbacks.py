"""MLflow callbacks for Ultralytics training.

The metrics callback logs every numeric column of ``results.csv`` each epoch, including the
losses Ultralytics' own integration can omit::

    from croi.detection.backends.ultralytics.mlflow_callbacks import register_full_metrics_callback
    register_full_metrics_callback(model)
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path

import mlflow
from mlflow.exceptions import MlflowException

_ALLOWED_RE = re.compile(r"[^A-Za-z0-9_\-./: ]")


def sanitize_metric_name(name: str) -> str:
    """Replace characters MLflow metric names reject ([A-Za-z0-9_-./: ] allowed)."""
    out = _ALLOWED_RE.sub("_", name)
    out = re.sub(r"_{2,}", "_", out)
    return out.strip("_")


def _log_results_row(trainer) -> None:
    """Log the latest row of ``trainer.save_dir/results.csv``."""
    csv_path = Path(trainer.save_dir) / "results.csv"
    if not csv_path.exists():
        return
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    last = {k.strip(): v.strip() for k, v in rows[-1].items() if k}

    try:
        epoch = int(float(last.get("epoch", trainer.epoch)))
    except (TypeError, ValueError):
        epoch = trainer.epoch

    for raw_key, raw_val in last.items():
        if raw_key == "epoch":
            continue
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            continue
        if math.isnan(val):
            continue
        try:
            mlflow.log_metric(sanitize_metric_name(raw_key), val, step=epoch)
        except MlflowException as e:  # a tracking-server error must not end the run
            print(f"  WARN: epoch {epoch} metric {raw_key!r} not logged to MLflow: {e}")


def register_full_metrics_callback(model) -> None:
    """Attach the per-epoch metric-logging callback to a YOLO model."""
    model.add_callback("on_fit_epoch_end", _log_results_row)


def register_artifact_sync_callback(model, period: int = 25) -> None:
    """Log the run dir, plots and checkpoints included, to the active MLflow run every ``period`` epochs.

    ``period <= 0`` disables it.
    """
    if not period or period <= 0:
        return

    def _sync(trainer) -> None:
        if (trainer.epoch + 1) % period != 0:
            return
        try:
            mlflow.log_artifacts(str(trainer.save_dir), artifact_path="run")
        except MlflowException as e:  # the end-of-run upload in `croi.train` still runs
            print(f"  WARN: epoch {trainer.epoch} run-dir sync to MLflow failed: {e}")

    model.add_callback("on_fit_epoch_end", _sync)


def register_run_id_persistence_callback(model, run_id_file) -> None:
    """Write the active MLflow run_id to ``run_id_file`` each epoch, for ``resumed_from``."""
    run_id_file = Path(run_id_file)

    def _persist(trainer) -> None:
        run = mlflow.active_run()
        if run is None:
            return
        run_id_file.parent.mkdir(parents=True, exist_ok=True)
        run_id_file.write_text(run.info.run_id)

    model.add_callback("on_fit_epoch_end", _persist)
