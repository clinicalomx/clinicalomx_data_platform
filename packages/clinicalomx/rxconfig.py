"""Reflex project file for the standalone Extract Explorer (`./run_extract_app.sh`).

The app is `clinicalomx.extract.app.web.standalone`: `make_app(live=None)`, demo data by default.
`scripts/dev/control-plane.sh` also serves it. No path prefix here; the Build app's
`packages/clinicalomx_pipelines/rxconfig.py` sets `/build`.
"""
import reflex as rx

config = rx.Config(
    app_name="explorer",
    app_module_import="clinicalomx.extract.app.web.standalone",
    telemetry_enabled=False,
    show_built_with_reflex=False,
)
