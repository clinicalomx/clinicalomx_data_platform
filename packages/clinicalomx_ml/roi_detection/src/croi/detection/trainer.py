"""The training contract: what a trainer is handed, what it returns, and when it may resume.

A trainer receives the stage's ``parameters:`` block, the dataset as ``croi.io.coco.CocoSplits``,
a stable run directory and the base checkpoint path. The ``train:`` block passes verbatim to the
framework's own ``train``. A trainer leaves one ended MLflow run for ``mlflow.last_active_run()``.
`croi.train` handles everything after the fit. Base checkpoint keys are named per framework
(``ultralytics_model_name``, ``rfdetr_model_name``).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from croi.io.coco import CocoSplits


@dataclass
class TrainingRun:
    """What a fit produced, in the terms ``train`` registers."""

    best: Path                  # the checkpoint to register
    save_dir: Path              # the run directory, logged to MLflow as `run/`
    test_metrics: dict[str, float] = field(default_factory=dict)  # logged as `test/<name>`
    resumed_from: str | None = None  # the prior segment's MLflow run id, on a resume


class Trainer(Protocol):
    """Anything that fits a base checkpoint on the platform dataset.

    ``framework`` is the `backends.DETECTORS` key that reads the result. ``arch`` names the base
    architecture in the governed key. ``pip_requirements`` is what the MLflow model declares.
    """

    framework: str
    pip_requirements: list[str]

    def __init__(self, params: dict) -> None: ...

    @property
    def arch(self) -> str: ...

    def fetch_base(self, dest_dir: Path) -> Path: ...

    def fit(self, dataset: CocoSplits, run_dir: Path, base: Path) -> TrainingRun | None: ...


def decide_resume(mode: Any, ckpt: Path, why_not: Callable[[], str | None]) -> bool:
    """Resume an interrupted run; never a finished or foreign one.

    ``mode`` is ``auto``, True or False. ``why_not`` returns a reason the checkpoint must not be
    resumed, or None. With ``mode`` True a reason raises.
    """
    if ckpt.exists() and mode in (True, "auto"):
        why_not = why_not()
        if why_not is None:
            return True
        if mode is True:
            raise RuntimeError(
                f"resume: true but {ckpt} {why_not}; "
                "bump `name` or set resume: false to retrain from scratch."
            )
        print(f"auto-resume: {ckpt} {why_not} - starting fresh instead.")
        return False
    if mode is True:
        raise FileNotFoundError(f"resume: true but no checkpoint at {ckpt}")
    return False
