"""Re-capture the UniProt fixtures. The only code under `tests/` that touches the network.

Not collected by pytest. Run by hand when `FIELDS` widens:

    uv run python tests/fixtures/recapture_uniprot.py

Keeps the three headers a citation needs and writes 2-space JSON, matching the existing files.
Never hand-edit a fixture: each must hold what a live fetch returns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.conftest import ACCESSIONS, FIXTURES  # noqa: E402

from clinicalomx_markers.sources.snapshot import http  # noqa: E402
from clinicalomx_markers.sources.uniprot import url_for  # noqa: E402

#: Headers a snapshot may be cited for.
KEEP_HEADERS = ("content-type", "x-uniprot-release", "x-uniprot-release-date")


def recapture(accession: str) -> Path:
    fetched = http(url_for(accession))
    blob = {
        "headers": {k: v for k, v in fetched.headers.items() if k in KEEP_HEADERS},
        "body": json.loads(fetched.text),
    }
    path = FIXTURES / f"{accession}.json"
    path.write_text(json.dumps(blob, indent=2))
    return path


def main() -> None:
    for accession in sorted(ACCESSIONS.values()):
        path = recapture(accession)
        print(f"captured {accession} -> {path}")


if __name__ == "__main__":
    main()
