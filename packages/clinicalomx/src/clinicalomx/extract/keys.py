"""Element keys inside a merged object: the route from the anchor, the subtype, and the element.

    slide -> TMA0 -> R0C0, bgnorm   ->  TMA0_Core_R0C0_bgnorm
    TMA0  -> R0C0,          bgnorm  ->  Core_R0C0_bgnorm
    slide -> TMA1 -> R0C0, bgnorm   ->  TMA1_Core_R0C0_bgnorm

Keys are permanent: downstream queries index by them.
"""
from __future__ import annotations

import re

from clinicalomx.geometry import roi_id_for

#: Characters a zarr group name may not hold; they become "_". `key_for` de-duplicates the result.
_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def sanitise(part: str) -> str:
    return _SAFE.sub("_", part).strip("_") or "x"


def path_from(anchor_name: str, descendant_name: str) -> tuple[str, ...]:
    """The Record-name segments between the anchor and the descendant, both exclusive."""
    anchor = anchor_name.split(":")
    parts = descendant_name.split(":")
    if parts[:len(anchor)] == anchor:
        return tuple(parts[len(anchor):-1])
    # Not a name-prefix descendant: use the descendant's own leading segments.
    return tuple(parts[:-1])


def key_for(anchor_name: str, descendant_name: str, roi_type: str, element: str,
            *, taken=None) -> str:
    """The element key for one descendant inside a merged object anchored at ``anchor_name``.

    ``taken`` holds keys already issued; a collision gets a numeric suffix.
    """
    parts = [*path_from(anchor_name, descendant_name),
             roi_type, roi_id_for(descendant_name), element]
    key = "_".join(sanitise(p) for p in parts if p)
    if taken is None:
        return key
    unique, n = key, 2
    while unique in taken:
        unique, n = f"{key}-{n}", n + 1
    taken.add(unique)
    return unique
