"""Build app: the clinicalomx Extract Explorer served as a control-plane app.

Registers the Prefect orchestrator at import, then builds the app against the live instance.
"""
from __future__ import annotations

from clinicalomx.extract.app.web import make_app
from clinicalomx.extract.submit import use_prefect

# Must run before make_app, so the Build tab shows the submit button.
use_prefect()

app = make_app(
    live=True,
    title="Build",
    subtitle="What any ROI can be turned into, and a plan to build it. Builds run on the HPC.")
