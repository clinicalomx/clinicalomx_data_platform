"""ImagingChannel records on a throwaway instance: created once, usable as a claim subject.
The offline half is `tests/test_imaging_channel.py`."""

from __future__ import annotations

import lamindb as ln
from clinicalomx_ledger.grammar.store import LedgerClaimStore

from clinicalomx_markers.curate.channels import create_channel
from clinicalomx_markers.domain.imaging_channel import (
    CHANNEL_FEATURES,
    ImagingChannel,
    PLAChannel,
)
from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MARKER_TYPES,
    MarkerClaim,
    MarkerPredicate,
)
from clinicalomx_markers.vocab import VOCAB
from conftest import AGENT, BRANCH

ln.Feature(name=CHANNEL_FEATURES.PLATFORM,
           dtype=VOCAB.features[CHANNEL_FEATURES.PLATFORM].dtype).save()


def _records(channel: ImagingChannel | PLAChannel):
    return ln.Record.filter(name=channel.name, type__name=MARKER_TYPES.IMAGING_CHANNEL,
                            branch=ln.Branch.get(name=BRANCH))


def test_creating_a_channel_twice_leaves_one_record():
    channel = ImagingChannel(platform="PCF", antibody="4350003")
    first = create_channel(channel, branch=BRANCH)
    again = create_channel(ImagingChannel(platform="PCF", antibody="4350003"), branch=BRANCH)
    assert first.uid == again.uid
    assert _records(channel).count() == 1


def test_one_reagent_on_two_platforms_is_two_records():
    pcf = create_channel(ImagingChannel(platform="PCF", antibody="4350021"), branch=BRANCH)
    geomx = create_channel(ImagingChannel(platform="GeoMx", antibody="4350021"), branch=BRANCH)
    assert pcf.uid != geomx.uid
    assert pcf.features.get_values()[CHANNEL_FEATURES.PLATFORM] == "PhenoCycler-Fusion"
    assert (geomx.features.get_values()[CHANNEL_FEATURES.PLATFORM]
            == "NanoString digital spatial profiling")


def test_a_created_channel_is_a_claim_subject_on_the_ledger():
    channel = ImagingChannel(platform="PCF", stain="DAPI")
    create_channel(channel, branch=BRANCH)
    ledger = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **AGENT)
    claim = MarkerClaim(subject=channel.name, predicate=MarkerPredicate.SEGMENTATION_ROLE,
                        value="nucleus", source="human", source_id="rafael",
                        evidence="DAPI stains DNA.")
    ledger.add(claim)
    assert ledger.held(channel.name) == [claim]
    assert channel.name in ledger.in_state("unresolved")


def test_a_failed_platform_write_leaves_no_record(monkeypatch):
    import pytest

    from clinicalomx_markers.curate import channels

    class Undeclared:
        PLATFORM = "platform_never_declared"

    monkeypatch.setattr(channels, "CHANNEL_FEATURES", Undeclared)
    channel = ImagingChannel(platform="GeoMx", antibody="4350003")
    with pytest.raises(ln.errors.ValidationError):
        create_channel(channel, branch=BRANCH)
    assert _records(channel).count() == 0


def test_a_pla_channel_is_created_under_the_same_key_and_type():
    pla = PLAChannel(platform="PCF", probes=("4350003", "4350012"))
    first = create_channel(pla, branch=BRANCH)
    again = create_channel(PLAChannel(platform="PCF", probes=("4350012", "4350003")),
                           branch=BRANCH)
    assert first.uid == again.uid
    assert _records(pla).count() == 1
    assert first.features.get_values()[CHANNEL_FEATURES.PLATFORM] == "PhenoCycler-Fusion"
