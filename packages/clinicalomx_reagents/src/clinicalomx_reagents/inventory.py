"""The reagent inventory: antibodies, reporter barcodes, dyes. No model, and no claims.

An antibody's BX barcode and its reporter's RX barcode pair when their numbers are equal;
the pairing is derived, never stored. Checks here are relational: foreign keys, BX to RX
pairing, Akoya sets. Cell-level rules live in ``spec``.

``known_markers`` is injected. Target-claim checks live in
``clinicalomx_markers.domain.reagent_inventory``, which composes these into
``conjugate_problems``. Every row source is injected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rows import barcodes as _barcode_rows
from .spec import ANTIBODIES, BARCODES, FLUOROPHORES, NO_AKOYA_ID, column_problems

#: An antibody carries a BX barcode, its reporter the RX barcode of the same number.
ANTIBODY_BARCODE_PREFIX = "BX"
REPORTER_BARCODE_PREFIX = "RX"

Rows = list[dict[str, str]]

_BARCODE = re.compile(rf"^({ANTIBODY_BARCODE_PREFIX}|{REPORTER_BARCODE_PREFIX})(\d+)$")


@dataclass(frozen=True)
class Barcode:
    """A parsed barcode. ``number`` is what pairs an antibody with its reporter."""

    prefix: str
    number: int

    def __str__(self) -> str:
        return f"{self.prefix}{self.number:03d}"


def parse_barcode(value: str, *, expect: str | None = None) -> Barcode:
    """Parse ``BX002`` or ``RX002``, or raise. Case and leading zeros are not significant."""
    raw = (value or "").strip().upper()
    match = _BARCODE.match(raw)
    if not match:
        raise ValueError(
            f"{value!r} is not a barcode: expected "
            f"{ANTIBODY_BARCODE_PREFIX} or {REPORTER_BARCODE_PREFIX} followed by digits"
        )
    barcode = Barcode(prefix=match.group(1), number=int(match.group(2)))
    if expect is not None and barcode.prefix != expect:
        raise ValueError(f"{value!r} is a {barcode.prefix} barcode, expected {expect}")
    return barcode


def is_custom_conjugation(akoya_id: str) -> bool:
    """``NA`` means conjugated in house."""
    return (akoya_id or "").strip().upper() == NO_AKOYA_ID


def pairs(bx_barcode: str, rx_barcode: str) -> bool:
    """True if this reporter reveals this antibody, by the oligo number match."""
    antibody = parse_barcode(bx_barcode, expect=ANTIBODY_BARCODE_PREFIX)
    reporter = parse_barcode(rx_barcode, expect=REPORTER_BARCODE_PREFIX)
    return antibody.number == reporter.number


def reporter_for(bx_barcode: str, *, barcodes: Rows | None = None) -> dict[str, str] | None:
    """The reporter row that reveals this antibody, or None if none is stocked.

    Unreadable rows are skipped; :func:`key_problems` reports them.
    """
    antibody = parse_barcode(bx_barcode, expect=ANTIBODY_BARCODE_PREFIX)
    rows = _barcode_rows() if barcodes is None else barcodes
    for row in rows:
        try:
            reporter = parse_barcode(row.get("rx_barcode") or "",
                                     expect=REPORTER_BARCODE_PREFIX)
        except ValueError:
            continue
        if reporter.number == antibody.number:
            return row
    return None


def label(row: dict[str, str], column: str, position: int) -> str:
    """A row's key, or its position when the key is blank."""
    return (row.get(column) or "").strip() or f"row {position}"


def canonical_names(rows: Rows) -> set[str]:
    """The names a foreign key may reference. Blanks are excluded."""
    names = {(row.get("canonical_name") or "").strip() for row in rows}
    return names - {""}


def _keys(rows: Rows, column: str) -> list[str]:
    """The non-blank keys."""
    return [key for row in rows if (key := (row.get(column) or "").strip())]


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated)


def file_problems(antibodies: Rows, barcodes: Rows, fluorophores: Rows) -> list[str]:
    """Everything wrong cell by cell in each of the three files, in that order."""
    return (column_problems(ANTIBODIES, antibodies)
            + column_problems(BARCODES, barcodes)
            + column_problems(FLUOROPHORES, fluorophores))


def key_problems(antibodies: Rows, barcodes: Rows, fluorophores: Rows,
                 known_markers: set[str]) -> list[str]:
    """Unreadable and duplicate keys, and every foreign key that names nothing.

    ``known_markers`` is the canonical names an antibody's ``canonical_name`` may reference.
    """
    known_dyes = canonical_names(fluorophores)
    problems: list[str] = []

    for name in _duplicates(_keys(fluorophores, "canonical_name")):
        problems.append(f"fluorophores.csv: duplicate canonical_name {name!r}")

    reporter_keys: list[str] = []
    for position, row in enumerate(barcodes, start=1):
        name = label(row, "rx_barcode", position)
        try:
            reporter_keys.append(str(parse_barcode(row.get("rx_barcode") or "",
                                                   expect=REPORTER_BARCODE_PREFIX)))
        except ValueError as exc:
            problems.append(f"barcodes.csv: {name}: {exc}")
        dye = (row.get("fluorophore") or "").strip()
        if dye not in known_dyes:
            problems.append(f"barcodes.csv: {name} names unknown fluorophore {dye!r}")
    for key in _duplicates(reporter_keys):
        problems.append(f"barcodes.csv: duplicate rx_barcode {key!r}")

    antibody_keys: list[str] = []
    for position, row in enumerate(antibodies, start=1):
        name = label(row, "catalogue_number", position)
        try:
            antibody_keys.append(str(parse_barcode(row.get("bx_barcode") or "",
                                                   expect=ANTIBODY_BARCODE_PREFIX)))
        except ValueError as exc:
            problems.append(f"antibodies.csv: {name}: {exc}")
        target = (row.get("canonical_name") or "").strip()
        if target not in known_markers:
            problems.append(f"antibodies.csv: {name} names unknown marker {target!r}")
    for key in _duplicates(antibody_keys):
        problems.append(f"antibodies.csv: duplicate bx_barcode {key!r}")
    for number in _duplicates(_keys(antibodies, "catalogue_number")):
        problems.append(f"antibodies.csv: duplicate catalogue_number {number!r}")

    return problems


def _stated_sets(rows: Rows, column: str, expect: str) -> list[tuple[str, Barcode]]:
    """The rows that state a set membership, as (akoya_id, barcode). ``NA`` and unreadable
    barcodes are dropped."""
    stated = []
    for row in rows:
        akoya = (row.get("akoya_id") or "").strip().upper()
        if not akoya or is_custom_conjugation(akoya):
            continue
        try:
            stated.append((akoya, parse_barcode(row.get(column) or "", expect=expect)))
        except ValueError:
            continue
    return stated


def akoya_set_problems(antibodies: Rows, barcodes: Rows) -> list[str]:
    """Rows sharing an ``akoya_id`` must agree on the oligo number, and an antibody stocked with
    the reporter of its own number must carry that reporter's id."""
    antibody_sets = _stated_sets(antibodies, "bx_barcode", ANTIBODY_BARCODE_PREFIX)
    reporter_sets = _stated_sets(barcodes, "rx_barcode", REPORTER_BARCODE_PREFIX)

    members: dict[str, list[Barcode]] = {}
    for akoya, barcode in antibody_sets + reporter_sets:
        members.setdefault(akoya, []).append(barcode)

    problems = []
    for akoya, in_set in sorted(members.items()):
        if len({b.number for b in in_set}) > 1:
            spelled = ", ".join(sorted(str(b) for b in in_set))
            problems.append(
                f"antibodies.csv/barcodes.csv: akoya_id {akoya!r} names one set but covers "
                f"mismatched barcodes: {spelled}"
            )

    stocked: dict[int, set[str]] = {}
    for akoya, barcode in reporter_sets:
        stocked.setdefault(barcode.number, set()).add(akoya)
    for akoya, barcode in sorted(antibody_sets, key=lambda pair: (pair[1].number, pair[0])):
        for other in sorted(stocked.get(barcode.number, set()) - {akoya}):
            problems.append(
                f"antibodies.csv/barcodes.csv: {barcode} carries akoya_id {akoya!r} but its "
                f"reporter carries {other!r}: one set carries one id"
            )
    return problems
