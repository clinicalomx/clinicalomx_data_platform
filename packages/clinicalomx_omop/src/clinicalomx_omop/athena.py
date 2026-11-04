"""The OMOP standardised vocabularies, read from the user's own ATHENA download.

This repository ships no terminology: the files carry the SNOMED CT licence. `open_athena`
finds a download at a configured path, or says where to get one. Standard library only; a
lookup is one streaming pass over `CONCEPT.csv`.
"""

from __future__ import annotations

import csv
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .cdm import Table

#: Where the unzipped ATHENA download is looked for when no path is passed.
ENV = "CLINICALOMX_ATHENA_DIR"

#: The files this module reads. Others in the download are expected but unread.
REQUIRED = ("CONCEPT.csv", "VOCABULARY.csv")

#: The vocabulary this platform codes clinical values in, by its ATHENA `vocabulary_id`.
SNOMED = "SNOMED"

HOW_TO_OBTAIN = f"""\
This repository does not include the OMOP vocabularies and never will: they carry the
SNOMED CT licence, so every user brings their own copy.

To get one:
  1. Obtain a SNOMED CT licence. In a SNOMED International member country it is free
     through the National Release Centre (Australia: NCTS, https://www.healthterminologies.gov.au;
     elsewhere see https://www.snomed.org/our-stakeholders/members). Outside one, apply to
     SNOMED International for an affiliate licence (https://mlds.ihtsdotools.org).
  2. Register at ATHENA (https://athena.ohdsi.org), select the {SNOMED} vocabulary plus any
     others you code in, and download the bundle. ATHENA redistributes SNOMED CT under the
     same licence terms; it does not relax them.
  3. Unzip it and point {ENV} at the directory holding {' and '.join(REQUIRED)}.

Keep the download outside this repository and never commit any of its files."""


class VocabularyUnavailable(RuntimeError):
    """The ATHENA download is not where it was configured to be, or is not usable."""

    def __init__(self, problem: str) -> None:
        super().__init__(f"{problem}\n\n{HOW_TO_OBTAIN}")
        self.problem = problem


@dataclass(frozen=True)
class Concept:
    """One row of `CONCEPT.csv`. Held in memory only, never written anywhere."""

    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    concept_class_id: str
    standard_concept: str
    concept_code: str
    invalid_reason: str


def _rows(path: Path) -> Iterable[dict[str, str]]:
    # ATHENA writes bare quotes inside values, so quoting is off; fields hold no tabs.
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)


@dataclass(frozen=True)
class Athena:
    """An ATHENA download on disk, checked usable. Build with `open_athena`."""

    root: Path
    #: `vocabulary_version` per `vocabulary_id`, from `VOCABULARY.csv`.
    versions: Mapping[str, str]

    @property
    def snomed_version(self) -> str:
        return self.versions[SNOMED]

    def concepts(self, ids: Iterable[int]) -> dict[int, Concept]:
        """The concepts among `ids`, in one pass over `CONCEPT.csv`. Absent ids are absent."""
        wanted = {int(i) for i in ids}
        found: dict[int, Concept] = {}
        if not wanted:
            return found
        for row in _rows(self.root / "CONCEPT.csv"):
            cid = int(row["concept_id"])
            if cid in wanted:
                found[cid] = Concept(
                    concept_id=cid,
                    **{k: row[k] or "" for k in (
                        "concept_name", "domain_id", "vocabulary_id", "concept_class_id",
                        "standard_concept", "concept_code", "invalid_reason")},
                )
                if len(found) == len(wanted):
                    break
        return found


def open_athena(path: str | os.PathLike | None = None) -> Athena:
    """The ATHENA download at `path`, else at `$CLINICALOMX_ATHENA_DIR`.

    Raises `VocabularyUnavailable`, naming what is wrong and how to fix it, when neither is
    set, the directory is missing, a required file is missing, or SNOMED CT was not selected.
    """
    if path is None:
        configured = os.environ.get(ENV)
        if not configured:
            raise VocabularyUnavailable(f"No OMOP vocabulary is configured: {ENV} is not set.")
        root, how = Path(configured), f"{ENV}={configured}"
    else:
        root, how = Path(path), str(path)
    if not root.is_dir():
        raise VocabularyUnavailable(f"The OMOP vocabulary directory {how} does not exist.")
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise VocabularyUnavailable(
            f"{how} is not an unzipped ATHENA download: it has no {', '.join(missing)}.")
    versions = {r["vocabulary_id"]: r["vocabulary_version"]
                for r in _rows(root / "VOCABULARY.csv")}
    if SNOMED not in versions:
        raise VocabularyUnavailable(
            f"The ATHENA download at {how} does not include {SNOMED}: it was downloaded "
            "without selecting it, or without the licence ATHENA requires for it.")
    return Athena(root=root, versions=versions)


def check_concepts(athena: Athena, table: Table, rows: Iterable[Mapping]) -> list[str]:
    """Findings for every concept-valued field in `rows` of `table`; empty when all are good.

    A referenced concept must exist and, where the specification names a domain, be in it. `0`
    (no matching concept) is always allowed.
    """
    fields = [f for f in table.fields if f.fk_table == "concept"]
    rows = list(rows)
    used = {int(r[f.name]) for r in rows for f in fields if r.get(f.name) not in (None, 0)}
    known = athena.concepts(used)
    findings = []
    for i, row in enumerate(rows):
        for f in fields:
            value = row.get(f.name)
            if value in (None, 0):
                continue
            concept = known.get(int(value))
            if concept is None:
                findings.append(f"{table.name} row {i}: {f.name}={value} is not in the "
                                f"vocabulary at {athena.root}")
            elif f.fk_domain and concept.domain_id != f.fk_domain:
                findings.append(f"{table.name} row {i}: {f.name}={value} is a "
                                f"{concept.domain_id} concept; the CDM requires {f.fk_domain}")
    return findings
