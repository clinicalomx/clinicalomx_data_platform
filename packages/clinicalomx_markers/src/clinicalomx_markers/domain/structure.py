"""A structure is a GO cellular component, carried as a value. No model.

A structure has no record of its own: two markers in the cytoplasm share ``GO:0005737``. GO
is pinned at `seed_spec.GO_RELEASE`.

Structures are read from UniProt's curated localisation prose, mapped term by term through
``seed_data/structures.csv``, never from the broader ``go_c`` cross-references. A term the
table does not map is kept on `Reading.unmapped` and reported by `unmapped_terms`.

Imports no lamindb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clinicalomx.vocab.ontology import validate_pin

from ..seed_data.rows import structures as _structure_rows
from ..seed_spec import GO_ID, GO_RELEASE
from ..sources.uniprot import UniProtRecord

validate_pin("GO", GO_RELEASE)

_GO_ID = re.compile(GO_ID)


@dataclass(frozen=True)
class Structure:
    """A GO cellular component, as a value. Two equal identifiers are one structure.

    >>> Structure("GO:0005737", "cytoplasm").go_id
    'GO:0005737'
    """

    go_id: str
    #: GO's name for the term, for display only.
    name: str = ""
    release: str = GO_RELEASE

    def __post_init__(self) -> None:
        if not _GO_ID.fullmatch(self.go_id):
            raise ValueError(f"not a GO identifier: {self.go_id!r}")
        if self.release != GO_RELEASE:
            raise ValueError(f"{self.go_id} is named at GO {self.release!r}, not the pinned "
                             f"{GO_RELEASE}")


@dataclass(frozen=True)
class Mapped:
    """One prose term and the structure it was read as."""

    #: The UniProt comment's scope; `sources.uniprot.ENTRY_LEVEL` for the entry.
    scope: str
    #: The localisation term, verbatim from the curated prose.
    location: str
    structure: Structure


@dataclass(frozen=True)
class Reading:
    """What a record's curated prose says, as structures, with the gaps kept."""

    mapped: tuple[Mapped, ...] = ()
    #: ``(scope, term)`` pairs the prose states and the table does not map.
    unmapped: tuple[tuple[str, str], ...] = ()

    @property
    def go_ids(self) -> tuple[str, ...]:
        """Every structure named, first seen first, each once."""
        return tuple(dict.fromkeys(m.structure.go_id for m in self.mapped))


def mapping(rows: list[dict[str, str]] | None = None) -> dict[str, Structure]:
    """``structures.csv`` as ``{UniProt term: Structure}``. A term mapped twice raises."""
    table: dict[str, Structure] = {}
    for row in _structure_rows() if rows is None else rows:
        term = row["uniprot_location"].strip()
        if term in table:
            raise ValueError(f"{term!r} is mapped twice in structures.csv")
        table[term] = Structure(row["go_id"].strip(), row["go_name"].strip(),
                                row["go_release"].strip())
    return table


def read(record: UniProtRecord, table: dict[str, Structure] | None = None) -> Reading:
    """The structures ``record``'s curated localisation prose names, term by term."""
    table = mapping() if table is None else table
    mapped, unmapped = [], []
    for block in record.localisations:
        for term in block.locations:
            if term in table:
                mapped.append(Mapped(block.scope, term, table[term]))
            else:
                unmapped.append((block.scope, term))
    return Reading(tuple(mapped), tuple(unmapped))


def unmapped_terms(record: UniProtRecord, marker: str,
                   table: dict[str, Structure] | None = None) -> list[str]:
    """One finding per prose term ``record`` states that the table does not map. Empty is a pass."""
    return [f"{marker}: {term!r} ({scope}) has no GO structure; add a row to "
            f"seed_data/structures.csv"
            for scope, term in read(record, table).unmapped]
