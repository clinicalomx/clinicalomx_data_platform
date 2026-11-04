"""Session bootstrap: the disposable postgres:18 instance from `disposable.py`, then lamindb.

Runs at import time: test modules import the ledger models at module scope.
"""

from __future__ import annotations

import pytest

from disposable import (  # noqa: F401  (re-exported for the test modules)
    ROLES,
    activate_role_logins,
    as_role,
    start,
    superuser,
    table,
    verify_isolated,
)

_instance = start()
DB_URL = _instance.db_url
CONTAINER_ID = _instance.container_id

import lamindb  # noqa: E402, F401  (connects to the throwaway instance)

verify_isolated()

# Synthetic actor typing for service calls. Each call site names its own actor column.
HUMAN = {"actor_kind": "human"}
AGENT = {
    "actor_kind": "agent",
    "agent_model": "synthetic-model",
    "agent_model_version": "1",
}

activate_role_logins()


@pytest.fixture(scope="session")
def db_url() -> str:
    return DB_URL


@pytest.fixture(scope="session")
def container_id() -> str:
    return CONTAINER_ID
