"""RF-DETR: `Detector` reads a checkpoint, `trainer.Trainer` fits one.

`Trainer` is not imported here: it imports mlflow.
"""
from croi.detection.backends.rfdetr.detector import Detector

__all__ = ["Detector"]
