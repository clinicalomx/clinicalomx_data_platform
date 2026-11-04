"""Fine-tune an ultralytics YOLO, the `Trainer` for the ``ultralytics`` framework.

Ultralytics opens the MLflow run; `mlflow_callbacks` adds metrics, artifact sync and the run-id file.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from croi.detection.backends.ultralytics import yolo
from croi.detection.trainer import TrainingRun, decide_resume

if TYPE_CHECKING:
    from croi.io.coco import CocoSplits


def _norm_device(d) -> str:
    """Canonical device token: '', None and auto become 'auto'; 'cuda:0' becomes '0'."""
    s = str(d).strip().lower() if d is not None else ""
    if s in ("", "none", "auto"):
        return "auto"
    return s.replace("cuda:", "")


class Trainer:
    """Fine-tune an ultralytics YOLO."""

    framework = "ultralytics"
    pip_requirements = ["ultralytics"]

    def __init__(self, params: dict) -> None:
        self.params = params

    @property
    def base_name(self) -> str:
        return self.params.get("ultralytics_model_name", "yolo11n.pt")

    @property
    def arch(self) -> str:
        return Path(self.base_name).stem

    def fetch_base(self, dest_dir: Path) -> Path:
        from ultralytics.utils.downloads import attempt_download_asset

        dest = dest_dir / Path(self.base_name).name
        if not dest.exists():
            downloaded = Path(attempt_download_asset(self.base_name))
            if downloaded.resolve() != dest.resolve():
                shutil.copy2(downloaded, dest)
        return dest

    @staticmethod
    def _load_checkpoint(ckpt_path: Path) -> dict | None:
        import torch

        try:
            return torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
        except Exception:  # unreadable checkpoint: reported below
            return None

    def _why_not_resume(self, last_pt: Path) -> str | None:
        """Why ``last_pt`` must not be resumed, or None.

        A finished run (``epoch == -1``) is never resumed: ultralytics would fall back to its
        default ``coco8.yaml`` and train on COCO. A device change also blocks resume.
        """
        ck = self._load_checkpoint(last_pt)
        if ck is None:
            return "is unreadable/corrupt"
        epoch = ck.get("epoch", -1)
        if epoch is None or epoch < 0:
            return "is a COMPLETED run"
        ckpt_dev = _norm_device((ck.get("train_args") or {}).get("device"))
        requested = _norm_device(self.params["train"].get("device"))
        if ckpt_dev != requested:
            return f"device {ckpt_dev!r} != configured device {requested!r} (parameter change)"
        return None

    def fit(self, dataset: CocoSplits, run_dir: Path, base: Path) -> TrainingRun | None:
        from ultralytics import YOLO
        from ultralytics import settings as ultralytics_settings

        from croi.detection.backends.ultralytics.mlflow_callbacks import (
            register_artifact_sync_callback,
            register_full_metrics_callback,
            register_run_id_persistence_callback,
        )

        params = self.params
        name = params["name"]
        # A resume reads this data.yaml path back out of last.pt, so the location is fixed.
        data_yaml = yolo.write(dataset, run_dir.parent.parent / "_dataset" / name)
        ultralytics_settings.update({"mlflow": True})
        last_pt = run_dir / "weights" / "last.pt"
        resume = decide_resume(params.get("resume", "auto"), last_pt,
                                lambda: self._why_not_resume(last_pt))

        # Sync the run dir to MLflow every N epochs; 0 disables.
        log_period = params.get("mlflow_log_period", 25)
        run_id_file = run_dir / "mlflow_run_id.txt"
        resumed_from = None
        if resume:
            # A fresh MLflow run per resume segment: re-logging changed params on the original
            # run fails, since MLflow params are immutable, and tracking stops.
            resumed_from = run_id_file.read_text().strip() if run_id_file.exists() else None
            os.environ["MLFLOW_RUN"] = f"{name}_resume_{datetime.now():%Y%m%d_%H%M%S}"
            print(f"Resuming training from checkpoint {last_pt}"
                  + (f" (prior MLflow run {resumed_from})" if resumed_from else ""))
            model = YOLO(str(last_pt))
        else:
            print(f"Starting fresh training from base {base.name}")
            # Timestamped run name; the run directory stays stable.
            os.environ["MLFLOW_RUN"] = f"{name}_{datetime.now():%Y%m%d_%H%M%S}"
            model = YOLO(str(base))
        register_full_metrics_callback(model)
        register_artifact_sync_callback(model, period=log_period)
        register_run_id_persistence_callback(model, run_id_file)
        if resume:
            model.train(resume=True)
        else:
            model.train(
                data=str(data_yaml),
                project=str(run_dir.parent),
                name=run_dir.name,
                exist_ok=True,  # reuse runs/<name> so checkpoints stay resumable
                **params["train"],
            )

        save_dir = Path(model.trainer.save_dir)
        best_pt = save_dir / "weights" / "best.pt"
        if not best_pt.exists():
            print(f"ERROR: training finished but {best_pt} missing.")
            return None

        ultralytics_settings.update({"mlflow": False})
        test = YOLO(str(best_pt)).val(
            data=str(data_yaml), split="test",
            imgsz=params["train"].get("imgsz", 640), verbose=False,
        )
        ultralytics_settings.update({"mlflow": True})
        metrics = {k.replace("(B)", "").strip("/"): float(v)
                   for k, v in test.results_dict.items() if isinstance(v, (int, float))}
        return TrainingRun(best_pt, save_dir, metrics, resumed_from)
