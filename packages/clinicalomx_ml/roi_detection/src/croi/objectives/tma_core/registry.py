"""The ``tma_core`` champion line. ``load_champion`` is what ``clinicalomx_pipelines`` imports."""
from __future__ import annotations

from croi.io.registries import ChampionRegistry

REGISTRY = ChampionRegistry(registered_model="roi-core", alias_namespace="model_alias_core")

REGISTERED_MODEL = REGISTRY.registered_model
ALIAS_NAMESPACE = REGISTRY.alias_namespace

model_ref = REGISTRY.model_ref
resolve_weights = REGISTRY.resolve_weights
resolve_model_artifact = REGISTRY.resolve_model_artifact
load_champion = REGISTRY.load_champion
promote = REGISTRY.promote
resolve_champion = REGISTRY.resolve_champion
