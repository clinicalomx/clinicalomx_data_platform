"""The seam between the extract layer and whatever holds the data.

A :class:`Source` answers four questions: which entities match pushable predicates, which single
entity has a uid or name, what are the rows for some artifact ids and are their bytes there, and
what is the backend called. It never sees a recipe. The default is lamindb. External data is
ingested first; this is not a federation layer.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Selection:
    """The entity predicates a backend can push down. Mirrors :func:`select.narrow`.

    Tier-2 predicates (``cohort``, ``patient_id``, ``slide``) are not here.
    """

    roi_type: str | None = None
    name_contains: str | None = None
    has_roles: tuple[str, ...] = ()
    state: str | None = None
    parent_uid: str | None = None
    #: Maximum entities returned. Never unbounded.
    limit: int = 500


@dataclass(frozen=True)
class Artifact:
    """One artifact row, as a backend reports it.

    ``artifact_id`` is opaque: the storage key on lamindb. Fields a backend cannot answer stay
    ``None``; the lamindb backend leaves the entity fields unset.
    """

    artifact_id: str
    #: The entity this artifact belongs to: an ROI, a sample, a donor.
    entity_id: str | None = None
    #: Containment: what holds the entity.
    parent_entity_id: str | None = None
    role: str | None = None
    uri: str | None = None
    format: str | None = None
    qc_status: str | None = None
    parent_artifact_id: str | None = None
    #: Whether the backend found readable bytes.
    stored: bool = False


class Source(Protocol):
    """Where entities and artefacts are read from."""

    def slug(self) -> str:
        """The backend's name, stamped on every report's provenance block."""
        ...

    def entities(self, selection: Selection) -> list[Any]:
        """The entities matching ``selection``, capped at its limit. Handles are opaque."""
        ...

    def entity(self, *, uid: str | None = None, name: str | None = None,
               is_type: bool | None = None, order_by: str | None = None) -> Any | None:
        """One entity by uid or by name, or ``None``.

        A uid matching twice raises. A name lookup takes the first, ordered by ``order_by``.
        """
        ...

    def artifacts(self, artifact_ids: Iterable[str]) -> list[Artifact]:
        """Rows for the given ids, with bytes probed. One read per id. Unknown ids are omitted."""
        ...


#: The cached default backend. Creating it imports lamindb, which connects.
_DEFAULT: Source | None = None


def default_source() -> Source:
    """The lamindb backend, used when no ``source=`` is given."""
    global _DEFAULT
    if _DEFAULT is None:
        from clinicalomx.extract.lamin_source import LamindbSource

        _DEFAULT = LamindbSource()
    return _DEFAULT
