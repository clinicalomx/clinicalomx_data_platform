"""The TMA core detection record."""
from croi.io.objectives import DetectionObjective
from croi.objectives.tma_core.dataset import TmaCoreDataset
from croi.objectives.tma_core.registry import REGISTRY

TMA_CORE_OBJECT_DETECTION = DetectionObjective(
    name="tma_core",
    lamin_key_prefix="clinical-roi/core",
    registry=REGISTRY,
    dataset=TmaCoreDataset,
)
