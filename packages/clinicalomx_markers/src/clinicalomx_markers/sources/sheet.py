"""A vendor inventory sheet, read into columns. Deterministic, no model, no claims.

Only cleaning happens here; interpreting a column is `propose.columns`. ``.xlsx`` needs
openpyxl, the ``sheet`` extra, imported only when one is opened.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: How many of a column's most frequent values a profile lists.
TOP_VALUES = 5
#: Distinct cells a profile keeps for later transform previews. Beyond this, the
#: most frequent are kept and the profile says so.
CELLS_KEPT = 5000


@dataclass(frozen=True)
class SheetColumn:
    """One column of a sheet: its header as seen, where it is, and its cleaned cells."""

    #: The header cell exactly as the file holds it.
    header: str
    #: Zero-based position.
    position: int
    #: Every body cell, trimmed. Blank cells stay as ``""``.
    values: tuple[str, ...]
    sheet: str = "Sheet1"

    @property
    def name(self) -> str:
        """The trimmed header; blank when the column has none."""
        return self.header.strip()

    @property
    def address(self) -> str:
        """The header cell in A1 form, such as ``Sheet1!C1``."""
        return f"{self.sheet}!{column_letter(self.position)}1"

    @property
    def filled(self) -> tuple[str, ...]:
        return tuple(v for v in self.values if v)


def value_type(filled: tuple[str, ...]) -> str:
    """``number``, ``text``, ``mixed``, or ``empty`` for the filled cells. Numbers typed as text
    count as numbers."""
    if not filled:
        return "empty"
    numeric = sum(1 for v in filled if _is_number(v))
    return "number" if numeric == len(filled) else "text" if numeric == 0 else "mixed"


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def profile(column: SheetColumn, *, note: Callable[[str], str] | None = None,
            top: int = TOP_VALUES) -> dict:
    """Counts describing a column's cells, as shown to a reviewer.

    ``note`` flags one value for the proposer's intended slot, or returns ``""``. Ties keep file
    order. ``cells`` is every distinct cell with its count, blank included.
    """
    filled = column.filled
    counts = Counter(filled)
    every = Counter(column.values).most_common()
    values = []
    for value, count in counts.most_common(top):
        said = note(value) if note else ""
        values.append({"value": value, "count": count, **({"note": said} if said else {})})
    return {"header": column.header, "address": column.address, "sheet": column.sheet,
            "column": column_letter(column.position), "type": value_type(filled),
            "rows": len(column.values), "blank": len(column.values) - len(filled),
            "unique": len(counts), "top": values,
            "cells": [{"value": v, "count": n} for v, n in every[:CELLS_KEPT]],
            "cells_complete": len(every) <= CELLS_KEPT}


def column_letter(position: int) -> str:
    """``0`` -> ``A``, ``25`` -> ``Z``, ``26`` -> ``AA``, as a spreadsheet labels columns."""
    letters = ""
    n = position + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def header_key(header: str) -> str:
    """A header reduced to lower-case words: ``' Cat. No. '`` -> ``'cat no'``."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", header.casefold()).split())


def clean_cell(value: object) -> str:
    """One cell as trimmed text. ``None`` is blank. A number keeps its shortest spelling."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def columns_of(rows: list[list[object]], *, sheet: str = "Sheet1") -> list[SheetColumn]:
    """Columns from rows: the first row is the header, later rows are padded to its width.

    All-blank rows are dropped. A blank header still yields a column.
    """
    if not rows:
        return []
    header = [clean_cell(h) if h is None else str(h) for h in rows[0]]
    body = [[clean_cell(c) for c in row] for row in rows[1:]]
    body = [row for row in body if any(row)]
    width = max([len(header), *(len(r) for r in body)])
    header += [""] * (width - len(header))
    return [
        SheetColumn(header=header[i], position=i, sheet=sheet,
                    values=tuple(row[i] if i < len(row) else "" for row in body))
        for i in range(width)
        if header[i].strip() or any(i < len(row) and row[i] for row in body)
    ]


def read_rows(path: str | Path, *, sheet: str | None = None) -> tuple[str, list[list[object]]]:
    """``(sheet name, rows)`` of a ``.csv`` or the named (or first) sheet of an ``.xlsx``."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return sheet or path.stem, list(csv.reader(fh))
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ModuleNotFoundError as missing:
            raise ModuleNotFoundError(
                f"{path.name}: reading an .xlsx needs openpyxl; install "
                "clinicalomx_markers[sheet]") from missing
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = book[sheet] if sheet else book.worksheets[0]
            return ws.title, [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            book.close()
    raise ValueError(f"{path.name}: not a spreadsheet this reader opens (.csv, .xlsx)")


def read_columns(path: str | Path, *, sheet: str | None = None) -> list[SheetColumn]:
    name, rows = read_rows(path, sheet=sheet)
    return columns_of(rows, sheet=name)
