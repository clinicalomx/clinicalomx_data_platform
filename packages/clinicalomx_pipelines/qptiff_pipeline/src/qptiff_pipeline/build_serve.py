"""The Build app's production entry point: one process, one port, no Node at runtime.

Serves the bundle ``Dockerfile.build`` compiled:

    granian --interface asgi --host 0.0.0.0 --port 8601 qptiff_pipeline.build_serve:asgi

``asgi`` is built at import. Never import this module during ``reflex export``.
"""
from __future__ import annotations

from clinicalomx.extract.app.web.serve import asgi_app

from qptiff_pipeline.build_app import app

asgi = asgi_app(app)
