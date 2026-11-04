"""Fine-tune an RF-DETR, the `Trainer` for the ``rfdetr`` framework.

Opens the MLflow run itself and replays ``metrics.csv`` into it after the fit, so metrics arrive
at the end of the run, not live.
"""
from __future__ import annotations

import csv
import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import mlflow

from croi.detection.trainer import TrainingRun, decide_resume

if TYPE_CHECKING:
    from croi.io.coco import CocoSplits


class Trainer:
    """Fine-tune an RF-DETR from a published pretrained checkpoint.

    ``rfdetr_model_name`` is the published weights file (``rf-detr-nano.pth``, ...) and picks the
    architecture. ``train`` registers ``checkpoint_best_total.pth``; resume continues from
    ``last.ckpt``. ``FIT_COMPLETE`` marks a run whose fit returned.
    """

    framework = "rfdetr"
    pip_requirements = ["rfdetr"]

    #: Our split name -> the directory RF-DETR's reader expects.
    SPLIT_DIRS = {"train": "train", "val": "valid", "test": "test"}

    #: Written into the run directory when a fit returns, and removed when a fresh one starts.
    FIT_COMPLETE = "fit_complete"

    def __init__(self, params: dict) -> None:
        self.params = params

    @property
    def base_name(self) -> str:
        return self.params.get("rfdetr_model_name", "rf-detr-nano.pth")

    @property
    def arch(self) -> str:
        return Path(self.base_name).stem

    def fetch_base(self, dest_dir: Path) -> Path:
        from rfdetr.assets.model_weights import download_pretrain_weights

        dest = dest_dir / Path(self.base_name).name
        download_pretrain_weights(str(dest))  # no-op when present with the right MD5
        return dest

    @classmethod
    def roboflow_view(cls, dataset: CocoSplits, dest: Path) -> Path:
        """Write ``dataset`` in the Roboflow COCO layout under ``dest``, images as symlinks.

        ``dest`` is rebuilt. Adds ``area`` and ``iscrowd`` to every annotation. Returns ``dest``.
        """
        if dest.exists():
            shutil.rmtree(dest)
        for ours, theirs in cls.SPLIT_DIRS.items():
            split = getattr(dataset, ours)
            (dest / theirs).mkdir(parents=True)
            for image in split.images:
                (dest / theirs / image.file_name).symlink_to(Path(image.path))
            coco = split.model_dump(mode="json")
            for a in coco["annotations"]:
                a["area"] = a["bbox"][2] * a["bbox"][3]
                a["iscrowd"] = 0
            (dest / theirs / "_annotations.coco.json").write_text(json.dumps(coco))
        return dest

    def _why_not_resume(self, last_ckpt: Path) -> str | None:
        """Why ``last.ckpt`` must not be resumed, or None.

        A finished run, early-stopped or not, is never resumed: it would re-register the old
        checkpoint as new. ``FIT_COMPLETE`` tells it from an interrupted one.
        """
        if (last_ckpt.parent / self.FIT_COMPLETE).exists():
            return "is a COMPLETED run"
        import torch

        try:
            torch.load(str(last_ckpt), map_location="cpu", weights_only=False)
        except Exception:  # unreadable checkpoint: do not resume
            return "is unreadable/corrupt"
        return None

    def fit(self, dataset: CocoSplits, run_dir: Path, base: Path) -> TrainingRun | None:
        import rfdetr

        params = self.params
        name = params["name"]
        run_dir.mkdir(parents=True, exist_ok=True)
        last_ckpt = run_dir / "last.ckpt"
        resume = decide_resume(params.get("resume", "auto"), last_ckpt,
                                lambda: self._why_not_resume(last_ckpt))
        if not resume:
            (run_dir / self.FIT_COMPLETE).unlink(missing_ok=True)
            last_ckpt.unlink(missing_ok=True)
        run_id_file = run_dir / "mlflow_run_id.txt"
        resumed_from = run_id_file.read_text().strip() if resume and run_id_file.exists() else None
        view = self.roboflow_view(dataset, run_dir.parent / f"_rfdetr_{name}")

        label = "resume_" if resume else ""
        kwargs = {"run_test": True, "tensorboard": False, **params["train"],
                  "dataset_dir": str(view), "output_dir": str(run_dir)}
        if resume:
            kwargs["resume"] = str(last_ckpt)
        print(("Resuming training from checkpoint " + str(last_ckpt)) if resume
              else f"Starting fresh training from base {base.name}")
        with mlflow.start_run(run_name=f"{name}_{label}{datetime.now():%Y%m%d_%H%M%S}") as run:
            run_id_file.write_text(run.info.run_id)
            mlflow.log_params({k: v for k, v in params["train"].items()
                               if isinstance(v, (str, int, float, bool))})
            rfdetr.from_checkpoint(str(base)).train(**kwargs)
            (run_dir / self.FIT_COMPLETE).touch()
            rows = self._metric_rows(run_dir / "metrics.csv")
            for epoch, values in rows:
                for k, v in values.items():
                    if not k.startswith("test/"):
                        mlflow.log_metric(k.replace("@", "_"), v, step=epoch)

        best = run_dir / "checkpoint_best_total.pth"
        if not best.exists():
            print(f"ERROR: training finished but {best} missing.")
            return None
        test = {k[len("test/"):]: v for _, values in rows for k, v in values.items()
                if k.startswith("test/")}
        return TrainingRun(best, run_dir, test, resumed_from)

    @staticmethod
    def _metric_rows(csv_path: Path) -> list[tuple[int, dict[str, float]]]:
        """``[(epoch, {column: value})]`` from Lightning's ``metrics.csv``, merging sparse rows."""
        if not csv_path.exists():
            return []
        out = []
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                values = {}
                for k, v in row.items():
                    if k in ("epoch", "step") or v in ("", None):
                        continue
                    try:
                        x = float(v)
                    except ValueError:
                        continue
                    if not math.isnan(x):
                        values[k] = x
                if values:
                    out.append((int(float(row.get("epoch") or 0)), values))
        return out
