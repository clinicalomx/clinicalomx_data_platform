"""The Extract Explorer, as a Reflex app.

:func:`make_app` builds the whole application around :func:`view.body`. A host app calls it to
embed the real screen. ``live=None`` shows the demo/live toggle; a bool fixes the source and hides
the toggle. ``live`` is process state.
"""
from __future__ import annotations

import reflex as rx

from clinicalomx.extract.app.web import cache, state, tokens, view
from clinicalomx.extract.app.web.state import ExplorerState

__all__ = ["ExplorerState", "body", "make_app", "theme"]

body = view.body


def theme() -> rx.Component:
    """The Radix theme, passed straight through from the design contract."""
    return rx.theme(**tokens.THEME)


def make_app(*, live: bool | None = None, title: str = "Extract Explorer",
             subtitle: str = "", api=None) -> rx.App:
    """The whole application: page chrome, then the Explorer's body. Call once per process.

    ``live``: ``None`` offers the demo/live toggle; a bool fixes the source and hides it.
    ``title``: the heading and browser tab. ``subtitle``: one line under it, or nothing.
    ``api``: a Starlette application, such as ``clinicalomx_markers.api.v1.curation_api``,
    beneath which Reflex mounts, sharing one process and port.
    """
    # Before any query: allows lamindb's synchronous reads on the event loop.
    cache.allow_blocking_reads()
    state.FORCED_LIVE = live

    def index() -> rx.Component:
        return rx.el.div(
            rx.vstack(
                rx.el.h1(title, class_name="cx-title"),
                *([view.caption(subtitle)] if subtitle else []),
                body(),
                spacing=tokens.GAP["page"], align_items="start", width="100%"),
            class_name="cx-root")

    app = rx.App(
        theme=theme(),
        # Type faces by URL; `tokens.css` is inlined below.
        stylesheets=tokens.stylesheets(),
        # In the head, so the first paint is styled.
        head_components=[rx.el.style(tokens.stylesheet())],
        style=tokens.APP_STYLE,
        api_transformer=api,
    )
    app.add_page(index, route="/", title=title, on_load=ExplorerState.on_load)
    return app
