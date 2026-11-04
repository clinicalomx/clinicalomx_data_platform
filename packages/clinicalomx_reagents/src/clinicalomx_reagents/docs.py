"""Render the generated tables of ``data/README.md`` from the column spec.

Run ``python -m clinicalomx_reagents.docs`` after editing ``spec.py``; a test checks the file
matches. Text outside the two markers is hand written. ``clinicalomx_markers.seed_docs`` uses
:func:`render` too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from .rows import DATA, read
from .spec import REAGENT_FILES, Column, SeedFile

README = DATA / "README.md"

BEGIN = "<!-- BEGIN GENERATED from spec.py: python -m clinicalomx_reagents.docs -->"
END = "<!-- END GENERATED -->"


def _rule(column: Column) -> str:
    rules = []
    if column.key:
        rules.append("required, row label")
    elif not column.blank_ok:
        rules.append("required")
    if column.one_of:
        rules.append("one of " + ", ".join(f"`{v}`" for v in column.one_of))
    if column.pattern:
        rules.append(column.pattern_says)
    return ", ".join(rules) or "optional"


def render(specs: Iterable[SeedFile], count: Callable[[SeedFile], int]) -> str:
    """The generated block, without its markers."""
    files = ["| File | Rows | Holds |", "|---|---|---|"]
    columns = ["| File | Column | Holds | Rule |", "|---|---|---|---|"]
    for spec in specs:
        files.append(f"| `{spec.filename}` | {count(spec)} | {spec.holds} |")
        for column in spec.columns:
            columns.append(
                f"| `{spec.filename}` | `{column.name}` | {column.holds} | {_rule(column)} |"
            )
    return "\n".join([
        *files,
        "",
        "Every column below is validated when the file is read.",
        "A row that breaks one of these rules raises rather than resolving to a default.",
        "",
        *columns,
    ])


def block(readme: Path, begin: str, end: str = END) -> str:
    """The generated block as it stands in a README."""
    text = readme.read_text()
    start = text.index(begin) + len(begin)
    return text[start:text.index(end)].strip("\n")


def write(readme: Path, begin: str, generated: str, end: str = END) -> None:
    text = readme.read_text()
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    readme.write_text(f"{head}{begin}\n\n{generated}\n\n{end}{tail}")


def reagent_tables() -> str:
    return render(REAGENT_FILES.values(), lambda spec: len(read(DATA / spec.filename, spec)))


if __name__ == "__main__":
    write(README, BEGIN, reagent_tables())
    print(f"wrote {README}")
