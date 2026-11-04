"""Talking to the lamindb instance. Every module here needs a reachable instance.

  registry     idempotent get-or-create for Artifact / Record / type / Project
  linking      the two kinds of artifact link, plus run lineage
  queries      the front-door queries, keyed on this repo's storage layout and states
  tracking     opening a lamindb Run for a stage (the one prefect-aware module here)
  context_io   reading and writing typed detect contexts against Records

Select source artifacts by ``key__endswith``, never by ``Artifact.suffix``: lamindb 2.8 records
``.qptiff`` as an empty suffix.
"""
from __future__ import annotations
