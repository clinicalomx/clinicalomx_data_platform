"""Which vocabulary version this instance is at.

The applied version is a ``SeedVersion`` record named by a hash of the declared state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..names import TYPES

# `import lamindb` connects, so only applied_version() imports it.

#: Identifies the hashing scheme, so a changed hash is attributable to the scheme or the content.
_SCHEME = "clinicalomx-vocab/2"


def content_hash() -> str:
    """A stable short hash over the parsed, canonicalised declared state.

    Reordering rows, whitespace and ``#`` comments do not change it. Steps contribute their names,
    not their source.
    """
    from . import dataio
    from .steps import STEPS

    state = {
        "scheme": _SCHEME,
        "features": _canon(dataio.features(), "name"),
        "types": _canon(dataio.types(), "name"),
        # As declared, not expanded, so the hash is the same at every site.
        "storages": _canon(dataio.storages(expand=False), "root"),
        "sources": dataio.sources_lock(),
        "steps": [getattr(s, "NAME", s.__name__) for s in STEPS],
    }
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _canon(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Sort rows by ``key`` and strip string values, so formatting is not content."""
    cleaned = [
        {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        for row in rows
    ]
    return sorted(cleaned, key=lambda r: str(r.get(key, "")))


def applied_version() -> str | None:
    """The most recently applied vocabulary hash, or None if never seeded."""
    import lamindb as ln

    seed_type = ln.Record.filter(name=TYPES.SEED_VERSION, is_type=True).one_or_none()
    if seed_type is None:
        return None
    latest = ln.Record.filter(type=seed_type).order_by("-created_at").first()
    return None if latest is None else str(latest.name)


def stamp(rec) -> str:
    """Record the current content hash as applied. Idempotent."""
    seed_type = rec.type(TYPES.SEED_VERSION,
                         description="Applied clinicalomx vocabulary version.")
    version = content_hash()
    rec.record(version, seed_type,
               description=f"Hash of the parsed declared state ({_SCHEME}).")
    return version
