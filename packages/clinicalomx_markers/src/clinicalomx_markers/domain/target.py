"""Which proteins a marker binds: a target claim carrying a candidate set and its kind. No model.

A marker points at a candidate set of proteins, never one identity. The set's `TargetKind`
decides how the candidates' localisations combine, in `combination`:

    single             one protein            nothing to combine
    isoform_ambiguous  one of these           overlap
    cross_reactive     all of these at once   sum
    complex_member     part of a complex      overlap

Overlap is the default; sum must be declared, since it can invent a localisation.

The claim value is ``<kind>:<a>|<b>``, candidates sorted. Only `TargetSet` reads and writes
it. Imports no lamindb.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..seed_data.rows import targets as _target_rows
from ..seed_spec import CANDIDATE_SEPARATOR, UNIPROT_CANDIDATE, TargetKind
from .marker import MarkerClaim, MarkerPredicate, MarkerSource

_CANDIDATE = re.compile(UNIPROT_CANDIDATE)

#: Joins a target claim's kind to its candidates in the value.
KIND_SEPARATOR = ":"


class Combine(StrEnum):
    """How the candidates' localisations become the marker's."""

    #: Only what every candidate shares.
    OVERLAP = "overlap"
    #: Everything any candidate has.
    SUM = "sum"


#: The kinds that declare SUM. Every other kind overlaps.
SUMMED_KINDS = frozenset({TargetKind.CROSS_REACTIVE})


def combination(kind: TargetKind | str) -> Combine:
    """How ``kind`` combines localisations. The rule, once.

    >>> combination("cross_reactive"), combination("isoform_ambiguous")
    (<Combine.SUM: 'sum'>, <Combine.OVERLAP: 'overlap'>)
    """
    return Combine.SUM if TargetKind(kind) in SUMMED_KINDS else Combine.OVERLAP


def parent(candidate: str) -> str:
    """The gene product an IsoId is an isoform of. An accession is its own parent."""
    return candidate.split("-", 1)[0]


@dataclass(frozen=True)
class TargetSet:
    """A kinded candidate set: what a target claim asserts.

    Construction validates kind and candidates. Candidates are sorted; a repeat is refused.

    >>> TargetSet("cross_reactive", ("P08727", "P05787")).value
    'cross_reactive:P05787|P08727'
    >>> TargetSet.parse("single:P12830").candidates
    ('P12830',)
    """

    kind: TargetKind
    candidates: tuple[str, ...]

    def __post_init__(self) -> None:
        kind = TargetKind(self.kind)
        candidates = tuple(sorted(c.strip() for c in self.candidates))
        if not candidates:
            raise ValueError("a target set names at least one candidate")
        bad = [c for c in candidates if not _CANDIDATE.fullmatch(c)]
        if bad:
            raise ValueError(f"not a UniProt accession or IsoId: {', '.join(map(repr, bad))}")
        if len(set(candidates)) != len(candidates):
            raise ValueError(f"a candidate is repeated: {', '.join(candidates)}")
        if kind == TargetKind.SINGLE and len(candidates) != 1:
            raise ValueError(f"a single target names exactly one candidate, not "
                             f"{', '.join(candidates)}")
        if kind != TargetKind.SINGLE and len(candidates) < 2:
            raise ValueError(f"{kind} names several candidates; one is a single target")
        if kind == TargetKind.ISOFORM_AMBIGUOUS and len({parent(c) for c in candidates}) != 1:
            raise ValueError(f"isoform_ambiguous candidates are isoforms of one protein, not "
                             f"{', '.join(candidates)}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "candidates", candidates)

    @property
    def value(self) -> str:
        return f"{self.kind}{KIND_SEPARATOR}{CANDIDATE_SEPARATOR.join(self.candidates)}"

    @classmethod
    def parse(cls, value: str) -> TargetSet:
        """Read a claim value back, or raise naming what is wrong."""
        kind, separator, candidates = (value or "").partition(KIND_SEPARATOR)
        if not separator:
            raise ValueError(f"{value!r} is not a target set: expected <kind>:<candidates>")
        return cls(kind, tuple(candidates.split(CANDIDATE_SEPARATOR)))

    @property
    def parents(self) -> frozenset[str]:
        """The gene products the candidates belong to."""
        return frozenset(parent(c) for c in self.candidates)


def combine(target: TargetSet, locations: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The marker's localisation terms, from each candidate's, by the kind's `combination`.

    ``locations`` maps every candidate to its terms; a missing candidate raises. Order follows
    the candidates, first seen first.

    >>> ck = TargetSet("cross_reactive", ("P05787", "P08727"))
    >>> combine(ck, {"P05787": ["Cytoplasm"], "P08727": ["Cytoplasm", "Nucleus"]})
    ('Cytoplasm', 'Nucleus')
    >>> combine(TargetSet("complex_member", ck.candidates), {"P05787": ["Cytoplasm"],
    ...                                                      "P08727": ["Cytoplasm", "Nucleus"]})
    ('Cytoplasm',)
    """
    missing = [c for c in target.candidates if c not in locations]
    if missing:
        raise ValueError(f"no localisation for candidate {', '.join(missing)}, so the "
                         f"{target.kind} set cannot be combined without it")
    per_candidate = [tuple(locations[c]) for c in target.candidates]
    seen = list(dict.fromkeys(term for terms in per_candidate for term in terms))
    if combination(target.kind) == Combine.SUM:
        return tuple(seen)
    return tuple(term for term in seen if all(term in terms for terms in per_candidate))


def target_claim(marker: str, target: TargetSet, *, source: MarkerSource | str,
                 source_id: str, evidence: str, source_version: str = "") -> MarkerClaim:
    """The claim that ``marker`` binds ``target``."""
    return MarkerClaim(subject=marker, predicate=MarkerPredicate.TARGET, value=target.value,
                       source=source, source_id=source_id, evidence=evidence,
                       source_version=source_version)


def seed_claims(rows: list[dict[str, str]] | None = None) -> list[MarkerClaim]:
    """``targets.csv`` as target claims, one per row, attributed to ``curated_by``. Invalid rows
    raise."""
    return [
        target_claim(row["canonical_name"].strip(),
                     TargetSet(row["kind"].strip(),
                               tuple(row["candidates"].split(CANDIDATE_SEPARATOR))),
                     source=MarkerSource.HUMAN, source_id=row["curated_by"].strip(),
                     evidence=row["evidence"].strip())
        for row in (_target_rows() if rows is None else rows)
    ]
