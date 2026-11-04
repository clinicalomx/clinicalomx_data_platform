"""Create an ImagingChannel or PLAChannel record on the instance, once per key.

The record is named `ImagingChannel.name`, so a rerun is a no-op. The platform is written as
its feature, holding the EFO term at the pinned release. The marker is derived, never
written. Imports lamindb only inside functions.
"""

from __future__ import annotations

from ..domain.imaging_channel import (
    CHANNEL_FEATURES,
    PLATFORM_SOURCE,
    PLATFORM_TERMS,
    ImagingChannel,
    PLAChannel,
)
from ..domain.marker import MARKER_TYPES


def create_channel(channel: ImagingChannel | PLAChannel, *, branch: str):
    """The channel's record on ``branch``, created on a miss and returned on a hit.

    The lookup names the branch explicitly. The record and its platform are written in one
    transaction.
    """
    import lamindb as ln
    from django.db import transaction

    platform = platform_term(channel.platform)
    on = ln.Branch.filter(name=branch).one_or_none()
    if on is None:
        raise ValueError(f"no branch named {branch!r} in this instance")
    found = ln.Record.filter(name=channel.name, type__name=MARKER_TYPES.IMAGING_CHANNEL,
                             branch=on).one_or_none()
    if found is not None:
        return found
    kind = ln.Record.filter(name=MARKER_TYPES.IMAGING_CHANNEL, is_type=True).one()
    with transaction.atomic():
        record = ln.Record(name=channel.name, type=kind, branch=on).save()
        record.features.add_values({CHANNEL_FEATURES.PLATFORM: platform})
    return record


def platform_term(label: str):
    """The EFO record ``label`` names, at the pinned release only.

    Raises when the term is absent or at another release: run `VOCAB.ensure()` first.
    """
    import bionty as bt
    from clinicalomx.vocab.ontology import pinned_source

    term = PLATFORM_TERMS.get(label)
    if term is None:
        raise ValueError(f"platform {label!r} has no EFO term declared in PLATFORM_TERMS")
    registry = getattr(bt, PLATFORM_SOURCE["entity"])
    pinned = pinned_source(PLATFORM_SOURCE["entity"], PLATFORM_SOURCE)
    found = registry.filter(ontology_id=term).one_or_none()
    if found is None:
        raise ValueError(f"{term} ({label}) is not imported at efo {pinned.version}; "
                         "run the vocabulary's ensure()")
    if found.source_id != pinned.id:
        held = ("no source" if found.source is None
                else f"{found.source.name} {found.source.version}")
        raise ValueError(f"{term} ({label}) is held at {held}, not the pinned efo "
                         f"{pinned.version}; the vocabulary's ensure() relinks it")
    return found
