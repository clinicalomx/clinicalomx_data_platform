"""The curation API's refusals that need no database. The ledger half is `tests_pg/test_api.py`."""

from __future__ import annotations

import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from clinicalomx_markers.api.tokens import Actor, Tokens, digest
from clinicalomx_markers.api.v1 import curation_api

ROUTES = [("post", "/api/v1/proposals"), ("get", "/api/v1/queue"),
          ("post", "/api/v1/decisions"), ("get", "/api/v1/lineage/marker/CD8A"),
          ("get", "/api/v1/releases"), ("post", "/api/v1/releases")]


@pytest.fixture
def client():
    tokens = Tokens({digest("human-token"): Actor(name="rafael", kind="human")})
    return TestClient(curation_api(tokens=tokens, branch="curation"))


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "human-token"}])
def test_every_route_refuses_an_anonymous_request(client, method, path, headers):
    answer = getattr(client, method)(path, headers=headers,
                                     **({"json": {}} if method == "post" else {}))
    assert answer.status_code == 401
    assert "anonymous" in answer.json()["error"]


def test_a_token_is_filed_by_digest_and_never_by_value(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(f'{{"{digest("s3cret")}": {{"actor": "bot", "kind": "agent", '
                    '"agent_model": "m", "agent_model_version": "1", "policy_version": "p"}}')
    tokens = Tokens.load(path)
    assert tokens.actor("s3cret") == Actor("bot", "agent", "m", "1", "p")
    assert tokens.actor(digest("s3cret")) is None
    with pytest.raises(ValueError, match="human or agent"):
        Actor("x", "robot")


def test_importing_the_api_connects_to_nothing():
    """Importing the routes does not import lamindb."""
    code = ("import sys, clinicalomx_markers.api.v1; "
            "print(sorted(m for m in ('lamindb', 'django') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"
