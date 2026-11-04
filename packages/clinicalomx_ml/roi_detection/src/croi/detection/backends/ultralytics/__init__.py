"""Ultralytics YOLO: `Detector` reads a checkpoint, `trainer.Trainer` fits one.

`Trainer` is not imported here: it imports mlflow.
"""
from croi.detection.backends.ultralytics.detector import Detector

__all__ = ["Detector"]
