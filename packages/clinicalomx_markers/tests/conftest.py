"""Offline test environment, also importable by the notebook prototype.

Fixtures are captured UniProt responses (`tests/fixtures/uniprot`): no network, model or
database.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinicalomx_markers.sources.snapshot import FetchedContent, from_mapping
from clinicalomx_markers.sources.uniprot import url_for

FIXTURES = Path(__file__).parent / "fixtures" / "uniprot"

#: canonical marker -> UniProt accession, for the captured fixtures.
ACCESSIONS = {
    "E-CADHERIN": "P12830",
    "NAKATPASE": "P05023",
    "VIMENTIN": "P08670",
    "CITRATE_SYNTHASE": "O75390",
    "LDHA": "P00338",
    "FOXP3": "Q9BZS1",
    "CD8A": "P01732",
}


def values_of(cls) -> list[str]:
    """Every name defined on a vocabulary class (TYPES, FEATURES, or a predicate or source enum),
    including inherited ones."""
    return [getattr(cls, k) for k in dir(cls)
            if not k.startswith("_") and isinstance(getattr(cls, k), str)]


def load_fixture(accession: str) -> FetchedContent:
    blob = json.loads((FIXTURES / f"{accession}.json").read_text())
    return FetchedContent(text=json.dumps(blob["body"]), headers=blob.get("headers", {}))


def fixture_transport():
    """Transport serving the cached responses, and raising for anything uncached."""
    return from_mapping({url_for(acc): load_fixture(acc) for acc in ACCESSIONS.values()})


@pytest.fixture
def transport():
    return fixture_transport()


@pytest.fixture
def snapshot_root(tmp_path) -> Path:
    """Where `sources.snapshot.capture` writes."""
    return tmp_path / "snapshots"
