"""Reflex project file for the standalone curation review page (`../run_curation_app.sh`)."""

import reflex as rx

config = rx.Config(
    app_name="curation",
    app_module_import="clinicalomx_markers.api.serve",
    telemetry_enabled=False,
    show_built_with_reflex=False,
)
