"""Serving a built Reflex app from one Python process, for a container.

Does what `reflex run --env prod` does without rebuilding the frontend: sets its three flags
through `environment.<VAR>.set` and returns the ASGI app for granian or uvicorn. A missing
`.web/build/client` makes every page 404 while `/ping` answers. HTML responses get
``Cache-Control: no-cache``; see `_revalidate_html`.
"""
from __future__ import annotations

from typing import Any


def asgi_app(app: Any):
    """The ASGI application for a production container, from an `rx.App` and a built frontend."""
    from reflex_base import constants
    from reflex_base.environment import environment

    environment.REFLEX_ENV_MODE.set(constants.Env.PROD)
    environment.REFLEX_MOUNT_FRONTEND_COMPILED_APP.set(True)
    environment.REFLEX_SKIP_COMPILE.set(True)
    return _revalidate_html(app())


def _revalidate_html(app):
    """Add ``Cache-Control: no-cache`` to HTML responses, so browsers load a redeployed image.

    Assets are content-hashed and cached; the HTML shell is not hashed and must revalidate.
    """
    from starlette.datastructures import MutableHeaders

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def sending(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/html"):
                    headers["cache-control"] = "no-cache"
            await send(message)

        return await app(scope, receive, sending)

    return wrapped
