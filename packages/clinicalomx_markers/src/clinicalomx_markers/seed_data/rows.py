"""The seed CSVs beside this module, validated against their column spec, one function per
file. They bootstrap the instance, which then holds the registry."""

from __future__ import annotations

from pathlib import Path

from clinicalomx_reagents.rows import read

from ..seed_spec import SEED_FILES

SEED_DATA = Path(__file__).parent


def seed_rows(name: str) -> list[dict[str, str]]:
    """Rows of one seed file, validated against its column spec. Raises on a mismatch.

    Read with ``clinicalomx_reagents.rows.read``.
    """
    return read(SEED_DATA / name, SEED_FILES[name])


def markers() -> list[dict[str, str]]:
    return seed_rows("markers.csv")


def targets() -> list[dict[str, str]]:
    """Seed target claims, as rows. `domain.target.seed_claims` reads them as claims."""
    return seed_rows("targets.csv")


def channels() -> list[dict[str, str]]:
    return seed_rows("channels.csv")


def structures() -> list[dict[str, str]]:
    """The prose-to-GO mapping. `domain.structure.mapping` reads it as values."""
    return seed_rows("structures.csv")


def rules() -> list[dict[str, str]]:
    return seed_rows("rules.csv")

