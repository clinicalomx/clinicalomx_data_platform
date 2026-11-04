"""A protein interaction: what a proximity-ligation channel measures. No model.

It localises to the overlap of its members' localisations, computed by
`domain.target.combine` as a ``complex_member`` set. Imports no lamindb.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..seed_spec import TargetKind
from .target import TargetSet, combine

#: The target kind an interaction combines as. Must overlap.
INTERACTION_KIND = TargetKind.COMPLEX_MEMBER

#: What joins an interaction's members in its name.
MEMBER_SEPARATOR = "&"


@dataclass(frozen=True)
class ProteinInteraction:
    """Two or more proteins that a signal measures together, named by UniProt accession.

    Members are valid accessions or IsoIds, sorted, distinct, and at least two.

    >>> ProteinInteraction(("P12830", "P05023")).members
    ('P05023', 'P12830')
    >>> ProteinInteraction(("P12830", "P05023")).name
    'P05023&P12830'
    """

    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", self.as_target.candidates)

    @property
    def as_target(self) -> TargetSet:
        """The members as a candidate set."""
        return TargetSet(INTERACTION_KIND, tuple(self.members))

    @property
    def name(self) -> str:
        """The members joined in sorted order."""
        return MEMBER_SEPARATOR.join(self.members)

    def localisation(self, locations: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
        """Where the interaction can be: only the terms every member shares.

        ``locations`` maps every member to its terms; a missing member raises. Members sharing
        nothing give an empty tuple.

        >>> ProteinInteraction(("P12830", "P05023")).localisation(
        ...     {"P12830": ["Cell membrane", "Cytoplasm"], "P05023": ["Cell membrane"]})
        ('Cell membrane',)
        """
        return combine(self.as_target, locations)
