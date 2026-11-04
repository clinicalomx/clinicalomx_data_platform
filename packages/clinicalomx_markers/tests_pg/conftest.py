"""Session setup for the ledger-backed store suite.

Starts a throwaway postgres:18 via testcontainers, inits a disposable lamin instance under a
scratch HOME with the bionty and clinicalomx_ledger modules, migrates it, and imports the
pinned EFO release, which downloads from bionty's public assets: needs network. Never
touches a live instance. Runs at import time, before any ledger model loads.

Outside `testpaths`. Install with `uv sync --group pg`, run with `uv run pytest tests_pg`.
"""

from __future__ import annotations

import atexit
import itertools
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest
from testcontainers.postgres import PostgresContainer

_pg = PostgresContainer("postgres:18")
_pg.start()
atexit.register(_pg.stop)

DB_URL = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")

_work = pathlib.Path(tempfile.mkdtemp(prefix="markers-ledger-tests-"))
(_work / "settings").mkdir()
(_work / "home").mkdir()
os.environ["LAMIN_SETTINGS_DIR"] = str(_work / "settings")
os.environ["HOME"] = str(_work / "home")

_lamin = pathlib.Path(sys.executable).parent / "lamin"
subprocess.run(
    [str(_lamin), "init", "--storage", "./store", "--db", DB_URL,
     "--modules", "bionty,clinicalomx_ledger"],
    cwd=_work, check=True,
)
subprocess.run([str(_lamin), "migrate", "deploy"], cwd=_work, check=True)

import lamindb as ln  # noqa: E402  (connects to the throwaway instance)
from clinicalomx_ledger.grammar.store import LedgerClaimStore, MemoryClaimStore  # noqa: E402

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MARKER_TYPES  # noqa: E402
from clinicalomx_markers.domain.reagent_mapping import MAPPING_TYPES  # noqa: E402

#: The curation branch every subject lives on.
BRANCH = "curation"

ln.Branch(name=BRANCH).save()

# The pinned ontology is loaded through the vocabulary's own declaration.
from clinicalomx.vocab.ontology import reconcile  # noqa: E402
from clinicalomx.vocab.upsert import Reconciler  # noqa: E402

from clinicalomx_markers.vocab import VOCAB  # noqa: E402

reconcile(Reconciler(), "ExperimentalFactor", VOCAB.sources["ExperimentalFactor"])

_TYPE_RECORDS = {
    name: ln.Record(name=name, is_type=True).save()
    for name in (MARKER_TYPES.MARKER, MARKER_TYPES.IMAGING_CHANNEL, "Fluorophore",
                 MAPPING_TYPES.ARTIFACT)
}

#: The session identity every agent-side write in this suite carries.
AGENT = {"actor": "curator-session", "actor_kind": "agent",
         "agent_model": "synthetic-model", "agent_model_version": "1"}

#: The same session run by a person. Only the actor fields differ.
HUMAN = {"actor": "rafael", "actor_kind": "human"}

_counter = itertools.count()


def subject(name: str, type_name: str = MARKER_TYPES.MARKER) -> str:
    """Register a claim subject on the curation branch."""
    branch = ln.Branch.get(name=BRANCH)
    if not ln.Record.filter(name=name, branch=branch).exists():
        ln.Record(name=name, type=_TYPE_RECORDS[type_name], branch=branch).save()
    return name


@pytest.fixture
def unique() -> str:
    """A per-test token: the ledger is shared and append-only."""
    return f"PG{next(_counter)}"


@pytest.fixture
def stores(unique):
    """The same fresh subject in a memory store and a ledger store, for parity walks."""
    name = subject(f"NAKATPASE-{unique}")
    memory = MemoryClaimStore(MARKER_DOMAIN)
    ledger = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **AGENT)
    return name, memory, ledger
