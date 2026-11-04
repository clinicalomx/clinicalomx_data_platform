"""One resolution of the instance, shared by ``describe`` and ``survey``.

An :class:`Index` memoises role availability per ``(record, role)``, readiness per
``(recipe, roi)``, observations per ROI type, and addressable child elements per parent. It is a
pure function of ``(snapshot, include_storage, probe keys)``, filled lazily and never
invalidated; replace the Snapshot to refresh. The backref map is instance-wide. The byte probe
covers only the caller's ``probe_keys``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from clinicalomx.extract import Recipe, all_recipes, providers
from clinicalomx.extract.availability import (
    Coverage,
    ElementAvailability,
    RecipeReadiness,
    RoleAvailability,
    _bytes_present,
    _child_elements,
    _Context,
    _coverage,
    _now,
    _readiness,
    _role_on,
    _type_of,
)
from clinicalomx.extract.observe import observe_all
from clinicalomx.names import FEATURES
from clinicalomx.snapshot import Snapshot
from clinicalomx.vocab.registry import owners as feature_owners

__all__ = ["Index", "declared_features"]


@dataclass
class Index:
    """The one resolution ``describe`` and ``survey`` aggregate. Build with :meth:`of`."""

    #: The Snapshot every answer derives from.
    snapshot: object
    #: The catalogue resolved at build time; the default when a caller names no recipes.
    catalogue: tuple[Recipe, ...]
    #: Who supplied that catalogue.
    providers: tuple[str, ...]
    #: When the underlying bulk reads hit the instance.
    built_at: str
    #: Whether artifact bytes were probed. ``missing`` is reported only when True.
    bytes_checked: bool
    ctx: _Context

    _readiness: dict = field(default_factory=dict, repr=False)
    _observed: dict = field(default_factory=dict, repr=False)
    _children: dict = field(default_factory=dict, repr=False)

    # Building.

    @classmethod
    def of(cls, snapshot=None, *, recipes=None, include_storage: bool = False,
           probe_keys=None, source=None, now: str | None = None) -> Index:
        """Resolve the reads both views share, once.

        ``include_storage`` probes the bytes of ``probe_keys``, which is then required.
        ``built_at`` is the Snapshot's read time, else now; ``now`` overrides both.
        """
        snap = snapshot if snapshot is not None else Snapshot()
        catalogue = tuple(recipes) if recipes is not None else tuple(all_recipes())
        declared = declared_features(snap)
        ctx = _Context(
            snap=snap,
            declared=declared,
            owners=feature_owners(),
            backrefs=_patched_backrefs(snap, declared),
            bytes_checked=include_storage,
        )
        if include_storage:
            if probe_keys is None:
                raise ValueError(
                    "include_storage needs probe_keys: the set of artifact keys to look for. "
                    "There is no safe default - probing nothing reports everything missing, and "
                    "probing the instance is one query per key.")
            ctx.present_bytes = _bytes_present(
                sorted(probe_keys), ctx.backrefs, source=source)
        # Stamped after the reads: the stamp is the age of the data.
        return cls(snapshot=snap, catalogue=catalogue, providers=providers(),
                   built_at=now or snap.read_at or _now(),
                   bytes_checked=include_storage, ctx=ctx)

    # The four tables.

    def role_on(self, record, role: str) -> RoleAvailability:
        """One grid cell. Resolved once per index, whoever asks."""
        return _role_on(role, record, self.ctx)

    def coverage(self, role: str, records) -> Coverage:
        """One role's spread over a set of ROIs. Pure arithmetic over :meth:`role_on`."""
        return _coverage(role, [(str(r.name), self.role_on(r, role).state) for r in records],
                         self.ctx, fallback_role=role)

    def readiness(self, recipe: Recipe, roi, *, inferred: tuple[str, ...] = ()) -> RecipeReadiness:
        """Whether one recipe could be built for one ROI. Keyed on the recipe object."""
        cell = (recipe, getattr(roi, "uid", roi))
        hit = self._readiness.get(cell)
        if hit is None:
            hit = self._readiness[cell] = _readiness(recipe, roi, self.ctx, inferred=inferred)
        return hit

    def observation(self, record, *, recipes=None):
        """What this ROI carries, from one bulk pass over its whole subtype."""
        catalogue = tuple(recipes) if recipes is not None else self.catalogue
        cell = (_type_of(record), catalogue)
        hit = self._observed.get(cell)
        if hit is None:
            hit = self._observed[cell] = observe_all(
                self.snapshot, roi_types=[cell[0]], recipes=list(catalogue),
                backrefs=self.ctx.backrefs)
        return hit.of(record)

    def child_elements(self, parent, children, *,
                       recipes=None) -> tuple[ElementAvailability, ...]:
        """Every element the catalogue can address on this parent's children."""
        catalogue = tuple(recipes) if recipes is not None else self.catalogue
        cell = (getattr(parent, "uid", parent), catalogue)
        hit = self._children.get(cell)
        if hit is None:
            hit = self._children[cell] = _child_elements(catalogue, list(children), self.ctx)
        return hit

    # Guards.

    def check(self, *, snapshot=None, include_storage: bool = False) -> None:
        """Raise if ``snapshot`` is not this index's, or bytes are wanted and were not probed."""
        if snapshot is not None and snapshot is not self.snapshot:
            raise ValueError(
                "this index was built over a different Snapshot. Pass the index alone, or build "
                "one over the snapshot you mean; an index is a pure function of its Snapshot and "
                "cannot be re-pointed.")
        if include_storage and not self.bytes_checked:
            raise ValueError(
                "this index did not probe artifact bytes, so it cannot report 'missing'. Build "
                "one with include_storage=True, or drop include_storage from this call.")


def declared_features(snap) -> frozenset[str]:
    """Every Artifact-valued name a role could resolve against.

    Role features, provenance features, and whatever the instance declares.
    """
    return (frozenset(snap.role_features())
            | frozenset(snap.provenance_features())
            | frozenset(snap.instance_artifact_features()))


def _patched_backrefs(snap, declared: frozenset[str]) -> dict[str, dict]:
    """``{artifact key: back-ref row}`` over every declared key on the instance.

    Adds provenance keys such as ``source_image``, which ``artifact_backrefs()`` omits.
    """
    backrefs = dict(snap.artifact_backrefs())
    seen: set[str] = set()
    for roi_type in snap.roi_types():
        for rec in snap.records(roi_type):
            for feature in declared:
                seen.update(snap.artifact_keys(rec, feature))
    unknown = sorted(seen - set(backrefs))
    if unknown:
        backrefs.update(snap.artifact_rows(
            unknown, [FEATURES.ROI_UID, FEATURES.ROI_NAME, FEATURES.ROI_TYPE]))
    return backrefs
