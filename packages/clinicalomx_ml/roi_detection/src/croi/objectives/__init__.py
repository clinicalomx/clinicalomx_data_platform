"""The objectives, one directory each, and ``get``, which finds one's record by name."""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from croi.io.objectives import DetectionObjective

_RECORDS = {"tma_core": "TMA_CORE_OBJECT_DETECTION", "tissue_objects": "TISSUE_OBJECT_DETECTION"}
NAMES = tuple(_RECORDS)


def get(name: str) -> DetectionObjective:
    """The record of the objective called ``name``, imported on demand."""
    return getattr(import_module(f"croi.objectives.{name}.objective"), _RECORDS[name])
