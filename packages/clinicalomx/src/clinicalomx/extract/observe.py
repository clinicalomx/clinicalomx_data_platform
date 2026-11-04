"""What an ROI actually carries, as distinct from what a recipe expects.

Reports only; never fills a slot. A bgnormed zarr's ``labels`` variable is the background model's
component map, not a segmentation, and a SpatialData output is also a ``.zarr``. An artifact no
recipe reads is reported for a human to name. :func:`observe_all` is bulk: one pass over a
Snapshot for every ROI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from clinicalomx.extract import all_recipes
from clinicalomx.extract.builders.kinds import natural_kind
from clinicalomx.snapshot import Snapshot


@dataclass(frozen=True)
class Seen:
    """One Artifact-valued feature on one ROI, as the instance has it."""

    feature: str
    keys: tuple[str, ...]
    #: Every key resolves to a registered Artifact. False means at least one dangles.
    registered: bool
    #: The kind the suffix implies, or ``None``. Never opens a file.
    kind: str | None
    #: True where some recipe element names this feature as a candidate role.
    claimed: bool
    #: True where an installed consumer Vocabulary declares it.
    declared_locally: bool

    def to_dict(self) -> dict:
        return {"feature": self.feature, "keys": list(self.keys), "registered": self.registered,
                "kind": self.kind, "claimed": self.claimed,
                "declared_locally": self.declared_locally}


@dataclass(frozen=True)
class Observation:
    """Everything one ROI actually carries."""

    uid: str
    name: str
    roi_type: str
    seen: tuple[Seen, ...] = ()

    @property
    def unclaimed(self) -> tuple[Seen, ...]:
        """Registered artifacts that no recipe reads."""
        return tuple(s for s in self.seen if s.registered and not s.claimed)

    @property
    def invisible_locally(self) -> tuple[Seen, ...]:
        """Present on the instance but declared by nothing installed here."""
        return tuple(s for s in self.seen if not s.declared_locally)

    def to_dict(self) -> dict:
        return {"uid": self.uid, "name": self.name, "roi_type": self.roi_type,
                "seen": [s.to_dict() for s in self.seen],
                "unclaimed": [s.feature for s in self.unclaimed],
                "invisible_locally": [s.feature for s in self.invisible_locally]}


@dataclass
class Observed:
    """The bulk result. Index by uid; never re-walk per ROI."""

    by_uid: dict[str, Observation] = field(default_factory=dict)
    #: Artifact-valued features the instance declares that nothing installed here does.
    instance_only: tuple[str, ...] = ()

    def of(self, record) -> Observation | None:
        return self.by_uid.get(getattr(record, "uid", record))

    def caveats(self) -> list[str]:
        """The caveats a report built on this must state."""
        out = []
        if self.instance_only:
            out.append(
                f"This instance declares {len(self.instance_only)} Artifact-valued feature(s) "
                f"that no vocabulary installed here does ({', '.join(self.instance_only[:5])}). "
                f"Their artifacts are real; only this environment cannot name their owner.")
        unclaimed = sorted({s.feature for o in self.by_uid.values() for s in o.unclaimed})
        if unclaimed:
            out.append(
                f"{len(unclaimed)} artifact kind(s) on these ROIs are read by no recipe "
                f"({', '.join(unclaimed[:5])}). They are NOT included in any plan. If one of them "
                f"is what you wanted, say so - nothing here will pick it up on your behalf.")
        return out


def _claimed_features(recipes) -> frozenset[str]:
    """Every feature the catalogue reads, as a role or as a placement input (`needs_features`)."""
    return frozenset(
        [role for r in recipes for e in r.elements for role in e.roles]
        + [f for r in recipes for e in r.elements for f in e.needs_features])


def observe_all(snapshot=None, *, roi_types=None, recipes=None, backrefs=None) -> Observed:
    """What every ROI carries, in one pass over the Snapshot's caches. Opens no file.

    Pass ``backrefs`` when holding a map wider than ``Snapshot.artifact_backrefs``, which omits
    provenance keys. :class:`clinicalomx.extract.index.Index` passes its own.
    """
    snap = snapshot if snapshot is not None else Snapshot()
    catalogue = list(recipes) if recipes is not None else all_recipes()
    claimed = _claimed_features(catalogue)

    from_instance = frozenset(snap.instance_artifact_features())
    from_installed = frozenset(snap.role_features()) | frozenset(snap.provenance_features())
    candidates = sorted(from_instance | from_installed)

    if backrefs is None:
        backrefs = snap.artifact_backrefs()
    by_uid: dict[str, Observation] = {}
    for roi_type in (list(roi_types) if roi_types else snap.roi_types()):
        for rec in snap.records(roi_type):
            seen = []
            for feature in candidates:
                keys = snap.artifact_keys(rec, feature)
                if not keys:
                    continue
                seen.append(Seen(
                    feature=feature,
                    keys=keys,
                    registered=all(backrefs.get(k) is not None for k in keys),
                    kind=natural_kind(keys[0]),
                    claimed=feature in claimed,
                    declared_locally=feature in from_installed,
                ))
            by_uid[rec.uid] = Observation(
                rec.uid, str(rec.name), roi_type, tuple(seen))

    return Observed(by_uid=by_uid, instance_only=tuple(sorted(from_instance - from_installed)))
