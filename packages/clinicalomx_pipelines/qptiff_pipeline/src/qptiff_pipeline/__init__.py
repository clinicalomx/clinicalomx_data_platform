"""qptiff_pipeline: the human-gated pipeline that runs ROI detection, bgnorm and registration.

It chains clinical_roi (detection), bgnorm (background normalisation) and clinicalomx (the lamindb
layer). This package holds the flows, the gate states and the Prefect deployments.

Subpackages, each importing only those above it: ``domain``, ``lamin``, ``orchestration``,
``checks``, ``flows``, ``review``. Where a change goes:

  feature         domain/vocab.py, with its allowed values
  state           the STAGES table in domain/states.py
  stage           STAGES, then flows/<name>.py, prefect.yaml and an edge in automations.py
  storage key     domain/keys.py
  data invariant  checks/pipeline.py, added to pipeline_checks()
  governed folder POLICIES in domain/governance.py

Never rename an event: the Prefect automations match on its name. Read a role feature with
``clinicalomx.newest_artifact`` or ``clinicalomx.all_artifacts``; it can hold several values.
"""
from __future__ import annotations

# No submodule imports here.

_RESOLVER_REGISTERED = False


def _model_resolver(**coord):
    # Lazy import: clinical_roi pulls in the ML stack.
    from clinical_roi.common.model_ref import load_model

    objective = coord.pop("objective", None)
    return load_model(objective, **coord) if objective else load_model(**coord)


def ensure_model_resolver() -> None:
    """Register the ``kind: model`` resolver with clinicalomx. Idempotent.

    Call it before the first ``.get()`` on a model InputRef.
    """
    global _RESOLVER_REGISTERED
    if _RESOLVER_REGISTERED:
        return
    from clinicalomx import register_model_resolver

    register_model_resolver(_model_resolver)
    _RESOLVER_REGISTERED = True
