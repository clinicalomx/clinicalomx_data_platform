"""clinicalomx: the lamindb foundation layer for the clinicalOMx instance.

Downstream repos depend on this rather than on lamindb directly. It provides:

- **Vocabulary**: :mod:`clinicalomx.names` (constants, no imports) and :mod:`clinicalomx.vocab`
  (the declared state and the ``plan | apply | check | audit`` seeder).
- **ROIs**: :mod:`clinicalomx.roi`. Every Artifact annotates one ROI Record, linked both ways.
  :mod:`clinicalomx.artifacts` reads the links back; :mod:`clinicalomx.geometry` finds a child's
  geometry row.
- **Environment**: :class:`Manifest`, the instance, Project, Storage and Services in
  ``manifest.yaml``.
- **Inputs**: :class:`InputRef`, the YAML form of ``ln.<Registry>.get()``.
- **Gates**: :mod:`clinicalomx.review`, human-in-the-loop stops stored as Record state.
- **Checks**: :mod:`clinicalomx.checks`, read-only data invariants behind one CLI.
- **Governance**: :mod:`clinicalomx.governance`, what may be read, ingested, derived from or
  trained on.

Vocabulary, checks, governance and recipes are discovered through entry points
(``clinicalomx.vocabulary``, ``clinicalomx.checks``, ``clinicalomx.governance``,
``clinicalomx.recipes``). A consumer that is not pip-installed advertises none; it registers at
runtime instead (:func:`register_policies`, ``clinicalomx.extract.register_recipes``).

``import lamindb`` connects to the instance, so every lamindb-touching name in ``__all__`` resolves
lazily on first access (:pep:`562`). Importing the package pulls in no orchestrator or model
framework; the model resolver and gate event emitter are injected.

A public name is a row in ``_EXPORTS``. Never remove a row: consumers outside this repository
import it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# names.py has no imports, so this is safe eagerly.
from clinicalomx.names import (
    ALL_FEATURES,
    ALL_TYPES,
    FEATURES,
    PROJECT,
    TYPES,
)

__version__ = "0.1.0"

#: Public name -> defining submodule, read by :func:`__getattr__`. A name absent here is not
#: importable from the package root.
_EXPORTS: dict[str, str] = {
    # ROI <-> Artifact relations
    "ROI_UID": "roi",
    "ROI_NAME": "roi",
    "ROI_TYPE": "roi",
    "PARENT_ROI": "roi",
    "annotate": "roi",
    "create": "roi",
    "ensure_features": "roi",
    "ensure_types": "roi",
    "get_or_create": "roi",
    "get_type": "roi",
    "merge": "roi",
    "require_roi": "roi",
    "resolve_roi": "roi",
    "split": "roi",
    "update": "roi",
    # reading an Artifact-valued feature
    "as_artifact": "artifacts",
    "all_artifacts": "artifacts",
    "newest_artifact": "artifacts",
    # geometry-parquet row identity
    "ROI_ID_COLUMN": "geometry",
    "roi_id_for": "geometry",
    "geometry_row": "geometry",
    # data governance
    "Policy": "governance",
    "UNDECLARED": "governance",
    "register_policies": "governance",
    "policy_for": "governance",
    "declared_folders": "governance",
    "permits_read": "governance",
    "permits_ingest": "governance",
    "permits_training": "governance",
    "permits_derive": "governance",
    # settings and environment declaration
    "YamlSettings": "settings",
    "Manifest": "manifest",
    "Project": "manifest",
    "Storage": "manifest",
    "Service": "manifest",
    "load_manifest": "manifest",
    # input refs
    "InputRef": "refs",
    "register_model_resolver": "refs",
    # model pointers (lamindb <-> MLflow)
    "ModelRef": "models",
    "DEFAULT_ALIAS": "models",
    "resolve_weights": "models",
    "resolve_model_artifact": "models",
    "resolve_mlflow_weights": "models",
    "download_best_pt": "models",
    "promote": "models",
    "alias_status": "models",
    "sync_alias": "models",
    "sync_aliases": "models",
    # environment pointers (lamindb <-> apptainer .sif)
    "EnvImage": "environments",
    "register_environment": "environments",
    "link_apptainer_to_run": "environments",
    "promote_environment": "environments",
    "sif_key_for_image": "environments",
    # config location
    "resolve_config": "config",
    "stage_config_dir": "config",
    "find_repo_root": "config",
    "CONFIG_DIR_ENV": "config",
    # idempotent materialization
    "existing_artifact": "idempotency",
    "materialize": "idempotency",
    # review gates
    "STATE_FEATURE": "review",
    "get_state": "review",
    "set_state": "review",
    "list_by_state": "review",
    "confirm": "review",
    "register_event_emitter": "review",
    "prefect_emitter": "review",
}

_EAGER = (
    "TYPES",
    "FEATURES",
    "ALL_TYPES",
    "ALL_FEATURES",
    "PROJECT",
    "__version__",
)

__all__ = [*_EAGER, *_EXPORTS]

if TYPE_CHECKING:  # pragma: no cover - import the real names for type checkers only
    from clinicalomx.artifacts import all_artifacts, as_artifact, newest_artifact
    from clinicalomx.config import (
        CONFIG_DIR_ENV,
        find_repo_root,
        resolve_config,
        stage_config_dir,
    )
    from clinicalomx.environments import (
        EnvImage,
        link_apptainer_to_run,
        promote_environment,
        register_environment,
        sif_key_for_image,
    )
    from clinicalomx.geometry import ROI_ID_COLUMN, geometry_row, roi_id_for
    from clinicalomx.governance import (
        UNDECLARED,
        Policy,
        declared_folders,
        permits_derive,
        permits_ingest,
        permits_read,
        permits_training,
        policy_for,
    )
    from clinicalomx.idempotency import existing_artifact, materialize
    from clinicalomx.manifest import Manifest, Project, Service, Storage, load_manifest
    from clinicalomx.models import (
        DEFAULT_ALIAS,
        ModelRef,
        alias_status,
        download_best_pt,
        promote,
        resolve_mlflow_weights,
        resolve_model_artifact,
        resolve_weights,
        sync_alias,
        sync_aliases,
    )
    from clinicalomx.refs import InputRef, register_model_resolver
    from clinicalomx.review import (
        STATE_FEATURE,
        confirm,
        get_state,
        list_by_state,
        prefect_emitter,
        register_event_emitter,
        set_state,
    )
    from clinicalomx.roi import (
        PARENT_ROI,
        ROI_NAME,
        ROI_TYPE,
        ROI_UID,
        annotate,
        create,
        ensure_features,
        ensure_types,
        get_or_create,
        get_type,
        merge,
        require_roi,
        resolve_roi,
        split,
        update,
    )
    from clinicalomx.settings import YamlSettings


def __getattr__(name: str):
    """Resolve a lamindb-touching export on first access (:pep:`562`)."""
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # cache
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
