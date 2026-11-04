"""The curation review page with the v1 API beneath it, in one process on one port.

Configured by environment:

    CURATION_TOKENS            path to the token digest file (`api.tokens`)
    CURATION_BRANCH            the branch subjects live on (`curation_api`'s ``branch``)
    CURATION_REQUIRE_DATABASE  optional ``host:port/name``; set, the process refuses to start
                               unless Django is connected to exactly that database

Needs the ``api`` extra and the ``app`` dependency group.
"""

from __future__ import annotations

import os

from clinicalomx.curation.app import make_app

from .tokens import Tokens
from .v1 import curation_api


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"set {name}; the curation API cannot be served without it "
                           "(see clinicalomx_markers.api.serve)")
    return value


def require_database(wanted: str) -> None:
    """Refuse unless Django's live connection is ``host:port/name``."""
    import lamindb  # noqa: F401  (connects to the instance this environment names)
    from django.conf import settings

    live = settings.DATABASES["default"]
    got = f"{live.get('HOST')}:{live.get('PORT')}/{live.get('NAME')}"
    if got != wanted:
        raise RuntimeError(f"connected to {got}, not {wanted}; refusing to serve curation "
                           "against any other database")


if wanted := os.environ.get("CURATION_REQUIRE_DATABASE", "").strip():
    require_database(wanted)

app = make_app(api=curation_api(tokens=Tokens.load(_required("CURATION_TOKENS")),
                                branch=_required("CURATION_BRANCH")))
