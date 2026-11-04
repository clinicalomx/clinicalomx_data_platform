"""The tissue object detection record."""
from croi.io.objectives import DetectionObjective
from croi.objectives.tissue_objects.dataset import TissueObjectsDataset
from croi.objectives.tissue_objects.registry import REGISTRY

TISSUE_OBJECT_DETECTION = DetectionObjective(
    name="tissue_objects",
    lamin_key_prefix="clinical-roi/general",
    registry=REGISTRY,
    dataset=TissueObjectsDataset,
)
