"""Render the generated tables of ``seed_data/README.md`` from the column spec.

Run ``python -m clinicalomx_markers.seed_docs`` after editing ``seed_spec.py``; a test checks
the file matches. Text outside the two markers is hand written.
"""

from __future__ import annotations

from pathlib import Path

from clinicalomx_reagents import docs

from .seed_data.rows import seed_rows
from .seed_spec import SEED_FILES

README = Path(__file__).parent / "seed_data" / "README.md"

BEGIN = "<!-- BEGIN GENERATED from seed_spec.py: python -m clinicalomx_markers.seed_docs -->"


def render() -> str:
    """The generated block, without its markers."""
    return docs.render(SEED_FILES.values(), lambda spec: len(seed_rows(spec.filename)))


def block() -> str:
    """The generated block as it stands in the README."""
    return docs.block(README, BEGIN)


def write() -> None:
    docs.write(README, BEGIN, render())


if __name__ == "__main__":
    write()
    print(f"wrote {README}")
