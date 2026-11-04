"""Driving the pipeline: rendering the topology to an orchestrator, plus gates and alerts.

  automations  Prefect Automations: event -> deployment, and flow-completion chains
  events       emit, the one way pipeline code publishes a domain event
  gate         the HITL gate - accept / reject / reopen, as a CLI and a library
  notify       operator alerts (Teams)

``python -m qptiff_pipeline.orchestration.automations`` registers the automations. It deletes and
recreates each by name, so do not run it while a unit is between stages.
"""
from __future__ import annotations
