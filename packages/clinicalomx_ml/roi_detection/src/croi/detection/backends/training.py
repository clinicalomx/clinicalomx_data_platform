"""Build a `Trainer` from a stage's ``parameters:`` block by its ``framework:``.

Imports mlflow; the production detector must not import it.
"""
from __future__ import annotations

from croi.config import DEFAULT_FRAMEWORK, Framework
from croi.detection.backends.rfdetr import trainer as rfdetr
from croi.detection.backends.ultralytics import trainer as ultralytics
from croi.detection.trainer import Trainer

#: `Trainer` classes by framework, constructed as `cls(params)`. Same keys as `backends.DETECTORS`.
TRAINERS: dict[Framework, type[Trainer]] = {
    "ultralytics": ultralytics.Trainer,
    "rfdetr": rfdetr.Trainer,
}


def trainer_for(params: dict) -> Trainer:
    """The trainer a ``parameters:`` block names, ``DEFAULT_FRAMEWORK`` when it names none."""
    framework = str(params.get("framework", DEFAULT_FRAMEWORK))
    if framework not in TRAINERS:
        raise ValueError(f"unknown framework {framework!r}; declared trainers: {sorted(TRAINERS)}")
    return TRAINERS[framework](params)
