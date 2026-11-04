"""What each reagent CSV may contain, declared once, and the machinery that declares it.

``data/`` holds three files: conjugated antibodies, their reporter barcodes, and the dyes.
Each is a :class:`SeedFile`: ``rows`` validates against it, ``inventory`` reuses it for a
vendor inventory, and ``docs`` generates ``data/README.md`` from it.

:class:`Column`, :class:`SeedFile` and :func:`column_problems` are generic;
``clinicalomx_markers.seed_spec`` uses them too. A column holds at most three single-cell
rules. Cross-row checks live in ``inventory``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SeedStatus(StrEnum):
    """Lifecycle of a curated seed row. Nothing is ``ACTIVE`` until the registry is reviewed."""

    ACTIVE = "active"
    DRAFT = "draft"


#: The ``akoya_id`` for a custom or in-house conjugation. Not a missing value.
NO_AKOYA_ID = "NA"

#: UniProt's own accession grammar, as UniProt publishes it.
UNIPROT_ACCESSION = r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}"

#: An IsoId: an accession plus an isoform suffix. ``P08575-8`` is a different molecule from
#: ``P08575``.
UNIPROT_ISOFORM = rf"(?:{UNIPROT_ACCESSION})-[0-9]+"


@dataclass(frozen=True)
class Column:
    """One column of one seed file, and the rules that apply to a single cell of it."""

    name: str
    holds: str
    #: The key column; names a row in a problem report.
    key: bool = False
    blank_ok: bool = True
    #: Appended to the blank complaint when a blank has a specific right answer.
    blank_hint: str = ""
    one_of: tuple[str, ...] = ()
    pattern: str = ""
    #: What ``pattern`` means, in words, for the problem report.
    pattern_says: str = ""


@dataclass(frozen=True)
class SeedFile:
    filename: str
    holds: str
    columns: tuple[Column, ...]

    @property
    def key(self) -> str:
        return next(c.name for c in self.columns if c.key)


STATUS = Column(
    "status",
    "active or draft. Draft rows are validated on every run but never seeded",
    blank_ok=False,
    one_of=tuple(SeedStatus),
)

ANTIBODIES = SeedFile(
    "antibodies.csv",
    "The reagent: catalogue number, vendor, target label, marker, isoform, BX barcode",
    (
        Column("catalogue_number", "The vendor's part number. One row per number", key=True,
               blank_ok=False),
        Column("vendor", "Who sells it"),
        Column("target_label", "The vendor's spelling of the target, kept verbatim"),
        Column("canonical_name", "The marker it binds, in this lab's spelling", blank_ok=False),
        Column("uniprot_isoform", "The exact IsoId the clone binds. Blank means not "
                                  "isoform specific",
               pattern=UNIPROT_ISOFORM,
               pattern_says="a UniProt IsoId such as P08575-8, or blank"),
        Column("bx_barcode", "The oligo it carries. Pairs with the RX of the same number",
               blank_ok=False),
        Column("akoya_id", "Akoya's product id for the antibody and reporter as a set",
               blank_ok=False,
               blank_hint=f"write {NO_AKOYA_ID} for a custom conjugation"),
        STATUS,
    ),
)

BARCODES = SeedFile(
    "barcodes.csv",
    "Reporter product identity: RX barcode, akoya id, fluorophore",
    (
        Column("rx_barcode", "The reporter oligo. Reveals the BX of the same number", key=True,
               blank_ok=False),
        Column("akoya_id", "Akoya's product id for the antibody and reporter as a set",
               blank_ok=False,
               blank_hint=f"write {NO_AKOYA_ID} for a custom conjugation"),
        Column("fluorophore", "The dye it carries, by canonical name", blank_ok=False),
        STATUS,
    ),
)

FLUOROPHORES = SeedFile(
    "fluorophores.csv",
    "Canonical dyes: excitation and emission peaks, FluoroFinder url",
    (
        Column("canonical_name", "The dye, named once per lab", key=True, blank_ok=False),
        Column("excitation_nm", "The vendor's nominal excitation peak, in nm"),
        Column("emission_nm", "The vendor's nominal emission peak, in nm"),
        Column("fluorofinder_url", "Spectra. Stays empty until someone has opened it"),
        Column("aliases", "Other spellings of the dye, pipe separated"),
        STATUS,
    ),
)

REAGENT_FILES: dict[str, SeedFile] = {
    spec.filename: spec for spec in (ANTIBODIES, BARCODES, FLUOROPHORES)
}


def column_problems(spec: SeedFile, rows: list[dict[str, str]]) -> list[str]:
    """Every cell-level problem in these rows."""
    problems = [
        f"{spec.filename}: missing column {column.name!r}"
        for column in spec.columns
        if any(column.name not in row for row in rows)
    ]
    for position, row in enumerate(rows, start=1):
        label = (row.get(spec.key) or "").strip() or f"row {position}"
        for column in spec.columns:
            if column.name not in row:
                continue
            problems += _cell_problems(spec, label, column, (row.get(column.name) or "").strip())
    return problems


def _cell_problems(spec: SeedFile, label: str, column: Column, value: str) -> list[str]:
    if not value:
        if column.blank_ok:
            return []
        hint = f"; {column.blank_hint}" if column.blank_hint else ""
        return [f"{spec.filename}: {label} has an empty {column.name}{hint}"]
    problems = []
    if column.one_of and value not in column.one_of:
        problems.append(
            f"{spec.filename}: {label} has {column.name} {value!r} not in "
            f"({', '.join(column.one_of)})"
        )
    if column.pattern and not re.fullmatch(column.pattern, value):
        problems.append(
            f"{spec.filename}: {label} has {column.name} {value!r}: "
            f"expected {column.pattern_says}"
        )
    return problems
