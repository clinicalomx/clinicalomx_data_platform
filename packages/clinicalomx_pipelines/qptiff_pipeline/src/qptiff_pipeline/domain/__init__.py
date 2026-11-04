"""What the pipeline is, independent of how it runs or where anything is stored.

Every module here imports with nothing but pydantic installed: no lamindb at module scope, no
prefect, no compute libraries. ``import lamindb`` connects to the instance.

  states      the state model: S / Decision / Gate, the STAGES topology, PipelineState
  vocab       every feature this repo declares, with its value domain
  keys        the storage key layout for derivatives
  contexts    the typed payloads a detect -> review -> persist hop hands on
  hitl        the review contract: Prefect artifact keys and editable classes per stage
  roi_io      GeoParquet IO that preserves .attrs
  governance  the per-folder policy table for this instance

Add a state inside the ``STAGES`` table in ``states``; ``GATES``, ``STAGE_ENTRY`` and
``ENTRY_EVENTS`` derive from it. Read feature names from ``vocab``, never ``states``. Add every new
declarer module to ``vocab._MODULES``. Import ``qptiff.py`` as ``qptiff_vocab``.
"""
from __future__ import annotations
