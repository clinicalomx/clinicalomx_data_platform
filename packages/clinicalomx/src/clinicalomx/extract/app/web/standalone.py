"""The standalone Explorer app: demo data by default, source toggle on the sidebar.

`rxconfig.py` names this module; Reflex imports that file before the app.
"""
from __future__ import annotations

from clinicalomx.extract.app.web import make_app

app = make_app(
    live=None,
    title="Extract Explorer",
    subtitle="What any ROI can be turned into, and a plan to build it.")
