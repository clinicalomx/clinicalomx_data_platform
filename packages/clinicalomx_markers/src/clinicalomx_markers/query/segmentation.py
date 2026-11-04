"""Candidate segmentation channels from approved roles. Image metrics choose among them.

Roles come from a release export (`clinicalomx_ledger.grammar.published`), which holds only
accepted roles. Undecided and rejected roles are never candidates. The caller loads the file.
"""

from __future__ import annotations

from dataclasses import dataclass

from clinicalomx_ledger.grammar.published import Published

from ..domain.imaging_channel import PLATFORM_SEPARATOR
from ..domain.marker import MARKER_DOMAIN, MarkerPredicate
from ..resolve import Kind, resolve
from ..seed_spec import SegmentationRole

#: Roles usable as whole-cell morphology channels.
STRUCTURAL_ROLES = (SegmentationRole.CYTO_STRUCTURAL,)
#: Roles usable for the nuclear channel.
NUCLEAR_ROLES = (SegmentationRole.NUCLEUS,)


@dataclass(frozen=True)
class Candidate:
    raw: str
    canonical: str
    platform: str
    segmentation_role: str


def _roles(release: Published) -> dict[str, str]:
    """Each marker's approved role; the newest accepted, last in the export, wins.

    Roles are written on the marker, so a role on a channel subject (``...@PCF``) is
    skipped.
    """
    if release.domain != MARKER_DOMAIN.name:
        raise ValueError(f"release {release.release_uid} is a {release.domain!r} export, "
                         f"not a {MARKER_DOMAIN.name!r} one")
    return {a.subject: a.value for a in release.answers
            if a.predicate == MarkerPredicate.SEGMENTATION_ROLE
            and PLATFORM_SEPARATOR not in a.subject}


def candidates(raw_channels: list[str], release: Published, *,
               platform: str = "PCF") -> list[Candidate]:
    """Resolve a panel's channel names and attach their approved imaging roles.

    Unresolved names, and markers with no approved role, are dropped.
    """
    roles = _roles(release)
    out = []
    for raw in raw_channels:
        got = resolve(raw, release, kind=Kind.MARKER)
        if not got.resolved or got.canonical not in roles:
            continue
        out.append(Candidate(
            raw=raw,
            canonical=got.canonical,
            platform=platform,
            segmentation_role=roles[got.canonical],
        ))
    return out


def structural(raw_channels: list[str], release: Published, *,
               platform: str = "PCF") -> list[Candidate]:
    """Candidate whole-cell morphology channels."""
    return [c for c in candidates(raw_channels, release, platform=platform)
            if c.segmentation_role in STRUCTURAL_ROLES]


def nuclear(raw_channels: list[str], release: Published, *,
            platform: str = "PCF") -> list[Candidate]:
    """Candidate nuclear channels."""
    return [c for c in candidates(raw_channels, release, platform=platform)
            if c.segmentation_role in NUCLEAR_ROLES]
