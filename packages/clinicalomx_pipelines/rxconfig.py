"""Reflex project file for the Build app.

The URL prefix and the port are compiled into the JavaScript bundle when the image is built.
Changing either one needs a rebuild.
"""
import reflex as rx

config = rx.Config(
    app_name="build",
    app_module_import="qptiff_pipeline.build_app",
    frontend_path="/build",
    backend_path="/build",
    # The operator portal's address, where the browser loads the app from.
    api_url="http://localhost:8080",
    telemetry_enabled=False,
    show_built_with_reflex=False,
)
