"""The app `run_curation_app.sh` serves answers `/api/v1` with JSON, not the page's HTML.

Built as Reflex builds it in production, from `curation_app/rxconfig.py`, with the compiled
frontend mounted. Content type and shape are checked: the frontend catch-all also answers.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from urllib.parse import urlsplit

import pytest
from clinicalomx.review import ensure_state_feature
from starlette.testclient import TestClient

from clinicalomx_markers.api.tokens import digest
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from conftest import BRANCH, DB_URL, subject

CURATION_APP = pathlib.Path(__file__).resolve().parents[2] / "clinicalomx" / "curation_app"
TOKEN = "standalone-human-token"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    tokens = tmp_path_factory.mktemp("tokens") / "tokens.json"
    tokens.write_text(json.dumps({digest(TOKEN): {"actor": "rafael", "kind": "human"}}))
    db = urlsplit(DB_URL)
    patch = pytest.MonkeyPatch()
    patch.chdir(CURATION_APP)
    patch.syspath_prepend(str(CURATION_APP))
    patch.setenv("CURATION_TOKENS", str(tokens))
    patch.setenv("CURATION_BRANCH", BRANCH)
    patch.setenv("CURATION_REQUIRE_DATABASE", f"{db.hostname}:{db.port}{db.path}")
    patch.setenv("REFLEX_ENV_MODE", "prod")
    patch.setenv("REFLEX_MOUNT_FRONTEND_COMPILED_APP", "true")
    (CURATION_APP / ".web" / "build" / "client").mkdir(parents=True, exist_ok=True)
    ensure_state_feature(feature=MARKER_DOMAIN.state_feature)
    sys.modules.pop("clinicalomx_markers.api.serve", None)
    serve = importlib.import_module("clinicalomx_markers.api.serve")
    try:
        yield TestClient(serve.app())
    finally:
        patch.undo()


def test_the_queue_is_json_with_the_contract_shape(served, unique):
    name = subject(f"STANDALONE-{unique}")
    made = served.post("/api/v1/proposals", headers={"Authorization": f"Bearer {TOKEN}"}, json={
        "domain": MARKER_DOMAIN.name, "subject": name,
        "predicate": str(MarkerPredicate.SEGMENTATION_ROLE), "value": "marker",
        "evidence": {"source": "human", "source_id": "rafael", "text": f"{name} on PCF"},
        "confidence": 0.9, "reasoning": "membrane stain"})
    assert made.status_code == 201, made.text

    got = served.get("/api/v1/queue", headers={"Authorization": f"Bearer {TOKEN}"})

    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("application/json"), got.text[:200]
    body = got.json()
    assert set(body) == {"items", "next"}
    assert name in {item["subject"] for item in body["items"]}


def test_a_refusal_under_the_api_is_json_not_the_page(served):
    """A call with no token gets a JSON 401."""
    got = served.get("/api/v1/queue")
    assert got.status_code == 401
    assert got.headers["content-type"].startswith("application/json")
