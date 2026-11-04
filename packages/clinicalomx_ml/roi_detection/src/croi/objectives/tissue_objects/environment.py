"""The ``tissue_objects`` binding of :mod:`croi.environment`.

Importing it registers this objective's ``kind: model`` InputRef resolver.
"""
from __future__ import annotations

from clinicalomx import register_model_resolver, stage_config_dir

from croi.environment import ObjectiveEnvironment, StageEnvironment, load_manifest

__all__ = ["CONFIGS", "ENVIRONMENT", "StageEnvironment", "load_config", "load_manifest",
           "preflight", "resolve_config"]

#: The second argument is the repo-relative fallback when run outside an install.
ENVIRONMENT = ObjectiveEnvironment(
    "tissue_objects",
    stage_config_dir(__file__, "roi_detection/src/croi/objectives/tissue_objects/configs"),
)

CONFIGS = ENVIRONMENT.configs
resolve_config = ENVIRONMENT.resolve_config
load_config = ENVIRONMENT.load_config
preflight = ENVIRONMENT.preflight

register_model_resolver(ENVIRONMENT.resolve_model)
