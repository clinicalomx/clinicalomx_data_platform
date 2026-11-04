"""Name to catalogue entity, a marker or a platform, deterministically. No model, no network,
no database.

    1. key collapse        lowercase, drop separators
    2. exact/alias lookup  canonical names, then accepted aliases
    3. model proposes      only for what survives here   -> propose/
    4. human approves      the alias is an accepted claim, served by the next release

Steps 1 and 2 are here. Key collapse changes only separators and case: `NakATPase` and
`Na-K-ATPase` become one key.

Canonical names come from the seeded markers and from the platform labels
`domain.imaging_channel.PLATFORM_TERMS` declares. Each kind is indexed separately. Aliases
come from a release export (`clinicalomx_ledger.grammar.published`), which holds only aliases
accepted when the release was cut. Only a marker can have an alias.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from clinicalomx_ledger.grammar.published import Published

from .domain.imaging_channel import PLATFORM_TERMS
from .domain.marker import MARKER_DOMAIN, MarkerPredicate
from .seed_data.rows import markers

# -- step 1: key collapse ------------------------------------------------------------------

_DROP = re.compile(r"[-_.\s/()]+")


def match_key(name: str) -> str:
    """Case- and separator-insensitive key for a raw channel or marker name.

    >>> match_key("Na-K-ATPase") == match_key("NakATPase")
    True
    >>> match_key("Pan-Cytokeratin")
    'pancytokeratin'
    """
    return _DROP.sub("", name.strip().lower())


# -- step 2: lookup ------------------------------------------------------------------------


class Kind(StrEnum):
    """What a resolved name names. One member per kind the catalogue holds."""

    #: The lab's canonical analyte; resolves to its canonical name.
    MARKER = "marker"
    #: The assay a channel ran on; resolves to its EFO term at `PLATFORM_SOURCE`.
    PLATFORM = "platform"


def _unique(pairs: list[tuple[str, str]]) -> dict[str, str | None]:
    """``match key -> id``. A key two different ids collapse to maps to None."""
    out: dict[str, str | None] = {}
    for name, ident in pairs:
        key = match_key(name)
        out[key] = ident if out.get(key, ident) == ident else None
    return out


@lru_cache(maxsize=1)
def _declared() -> dict[Kind, frozenset[tuple[str, str]]]:
    """Per kind, every ``(declared spelling, canonical id)``. A platform's id is its EFO term."""
    return {
        Kind.MARKER: frozenset((r["canonical_name"], r["canonical_name"]) for r in markers()),
        Kind.PLATFORM: frozenset(pair for label, term in PLATFORM_TERMS.items()
                                 for pair in ((label, term), (term, term))),
    }


@lru_cache(maxsize=1)
def _canonical() -> dict[Kind, dict[str, str | None]]:
    """Per kind, ``match key -> canonical id`` for every name the catalogue declares."""
    return {k: _unique(list(pairs)) for k, pairs in _declared().items()}


@lru_cache(maxsize=4)
def _aliases(release: Published) -> dict[str, str | None]:
    """``match key -> marker`` for every accepted alias in ``release``.

    A spelling accepted for two markers maps to None. A canonical name wins over an alias.
    """
    if release.domain != MARKER_DOMAIN.name:
        raise ValueError(f"release {release.release_uid} is a {release.domain!r} export, "
                         f"not a {MARKER_DOMAIN.name!r} one")
    return _unique([(a.value, a.subject) for a in release.answers
                    if a.predicate == MarkerPredicate.ALIAS])


def lookup(raw: str, release: Published | None = None, kind: Kind | None = None,
           ) -> tuple[Kind, str, str] | None:
    """``(kind, canonical id, via)`` for a raw spelling, or None if unresolved.

    ``via`` is "exact", "key" (after key collapse) or "alias". Without ``kind`` every kind is
    asked, and a spelling two kinds claim resolves to neither. Without a release only canonical
    names resolve.
    """
    key = match_key(raw)
    kinds = [kind] if kind is not None else list(Kind)
    hits = {(k, _canonical()[k][key]) for k in kinds if key in _canonical()[k]}
    if len(hits) > 1:
        return None
    if hits:
        (k, ident), = hits
        if ident is None:
            return None
        return (k, ident, "exact" if (raw, ident) in _declared()[k] else "key")
    if release is None or Kind.MARKER not in kinds:
        return None
    marker = _aliases(release).get(key)
    return (Kind.MARKER, marker, "alias") if marker is not None else None


def reset_cache() -> None:
    """Drop the memoised index. For tests that write data files."""
    _declared.cache_clear()
    _canonical.cache_clear()
    _aliases.cache_clear()


# -- the cascade ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    raw: str
    #: What ``canonical`` names: a marker's canonical name, or a platform's EFO term.
    kind: Kind
    canonical: str
    #: Which step answered: "exact", "key" or "alias".
    via: str

    resolved = True


@dataclass(frozen=True)
class Unresolved:
    raw: str
    key: str

    resolved = False
    kind = None
    canonical = None


def resolve(raw: str, release: Published | None = None, *, kind: Kind | None = None,
            ) -> Resolved | Unresolved:
    """Resolve a raw name to the marker or platform the catalogue says it names.

    ``kind`` narrows to one kind. ``release`` supplies approved aliases.
    """
    found = lookup(raw, release, kind)
    if found is None:
        return Unresolved(raw=raw, key=match_key(raw))
    found_kind, canonical, via = found
    return Resolved(raw=raw, kind=found_kind, canonical=canonical, via=via)


def resolve_all(raws: list[str], release: Published | None = None, *,
                kind: Kind | None = None) -> tuple[list[Resolved], list[Unresolved]]:
    """Partition a panel's channel names into resolved and unresolved."""
    out = [resolve(r, release, kind=kind) for r in raws]
    return ([r for r in out if r.resolved],  # type: ignore[misc]
            [r for r in out if not r.resolved])  # type: ignore[misc]
