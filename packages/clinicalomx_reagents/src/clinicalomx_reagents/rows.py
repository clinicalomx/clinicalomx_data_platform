"""The reagent CSVs in ``data/``, validated against their column spec. They bootstrap the
instance, which then holds the inventory. :func:`read` is also used for the marker seeds."""

from __future__ import annotations

import csv
from pathlib import Path

from .spec import ANTIBODIES, BARCODES, FLUOROPHORES, SeedFile, column_problems

DATA = Path(__file__).parent / "data"


def read(path: Path, spec: SeedFile) -> list[dict[str, str]]:
    """Rows of one seed file, validated against its column spec. Raises on a mismatch.

    A vendor inventory is reported instead: see ``inventory``.
    """
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="") as fh:
            body = (line for line in fh if not line.lstrip().startswith("#"))
            rows = [r for r in csv.DictReader(body) if any((v or "").strip() for v in r.values())]
    problems = column_problems(spec, rows)
    if problems:
        raise ValueError(
            f"{spec.filename} does not match its column spec:\n- " + "\n- ".join(problems)
        )
    return rows


def antibodies() -> list[dict[str, str]]:
    return read(DATA / ANTIBODIES.filename, ANTIBODIES)


def barcodes() -> list[dict[str, str]]:
    return read(DATA / BARCODES.filename, BARCODES)


def fluorophores() -> list[dict[str, str]]:
    return read(DATA / FLUOROPHORES.filename, FLUOROPHORES)
