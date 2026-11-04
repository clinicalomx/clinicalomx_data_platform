"""An ImagingChannel is its reagent plus its platform, and its marker is derived. Run against
the seed registry; the instance half is `tests_pg/test_channels.py`."""

from __future__ import annotations

import dataclasses

import pytest
from clinicalomx_ledger.grammar.store import MemoryClaimStore
from clinicalomx_reagents.rows import antibodies

from clinicalomx_markers.domain.imaging_channel import ImagingChannel
from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MARKER_TYPES,
    MarkerClaim,
    MarkerPredicate,
)
from clinicalomx_markers.seed_data.rows import markers

NAKATPASE_ANTIBODY = "4350012"


def test_one_reagent_on_two_platforms_is_two_channels():
    pcf = ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY)
    cosmx = ImagingChannel(platform="CosMx", antibody=NAKATPASE_ANTIBODY)
    assert pcf != cosmx
    assert pcf.name != cosmx.name
    assert len({pcf, cosmx}) == 2
    assert pcf.marker == cosmx.marker == "NAKATPASE"


def test_one_reagent_on_one_platform_twice_is_one_channel():
    first = ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY)
    again = ImagingChannel(platform=" PCF ", antibody=f" {NAKATPASE_ANTIBODY}")
    assert first == again
    assert first.name == again.name
    assert len({first, again}) == 1


def test_a_direct_stain_is_a_channel_and_is_its_own_marker():
    dapi = ImagingChannel(platform="PCF", stain="DAPI")
    assert dapi.name == "DAPI@PCF"
    assert (dapi.marker, dapi.fluorophore, dapi.reporter) == ("DAPI", "DAPI", None)


@pytest.mark.parametrize("row", antibodies(), ids=lambda r: r["catalogue_number"])
def test_every_antibody_channel_measures_the_marker_its_inventory_row_binds(row):
    channel = ImagingChannel(platform="PCF", antibody=row["catalogue_number"])
    assert channel.marker == row["canonical_name"]
    assert channel.reporter[2:] == row["bx_barcode"][2:], "BX and RX numbers pair"


def test_the_marker_cannot_be_stated():
    with pytest.raises(TypeError, match="marker"):
        ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY, marker="VIMENTIN")
    channel = ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        channel.marker = "VIMENTIN"
    with pytest.raises((ValueError, TypeError), match="init=False"):
        dataclasses.replace(channel, marker="VIMENTIN")


def test_the_reporter_and_dye_cannot_be_stated_either():
    for derived in ("reporter", "fluorophore"):
        with pytest.raises(TypeError, match=derived):
            ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY, **{derived: "x"})


def test_changing_the_platform_re_derives_rather_than_copying():
    pcf = ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY)
    moved = dataclasses.replace(pcf, platform="CosMx")
    assert moved.name.endswith("@CosMx")
    assert moved.marker == pcf.marker


@pytest.mark.parametrize("platform", ["", "  ", None])
def test_a_channel_without_a_platform_is_refused(platform):
    with pytest.raises(ValueError, match="platform"):
        ImagingChannel(platform=platform, antibody=NAKATPASE_ANTIBODY)


@pytest.mark.parametrize("reagent", [{}, {"antibody": NAKATPASE_ANTIBODY, "stain": "DAPI"}],
                         ids=["neither", "both"])
def test_a_channel_names_exactly_one_reagent(reagent):
    with pytest.raises(ValueError, match="exactly one"):
        ImagingChannel(platform="PCF", **reagent)


@pytest.mark.parametrize(("reagent", "message"), [
    ({"antibody": "no-such-catalogue"}, "no antibody"),
    ({"stain": "NOT_A_DYE"}, "no fluorophore"),
    ({"stain": "ATTO550"}, "not a marker"),
], ids=["unknown antibody", "unknown dye", "dye that is no marker"])
def test_a_reagent_the_registry_cannot_resolve_is_refused(reagent, message):
    with pytest.raises(ValueError, match=message):
        ImagingChannel(platform="PCF", **reagent)


def test_a_platform_cannot_forge_the_separator():
    with pytest.raises(ValueError, match="@"):
        ImagingChannel(platform="PCF@CosMx", stain="DAPI")


def test_no_channel_name_is_a_marker_name():
    """Channel and marker names share one column, so they never collide."""
    names = {r["canonical_name"] for r in markers()}
    channels = [ImagingChannel(platform="PCF", antibody=r["catalogue_number"])
                for r in antibodies()] + [ImagingChannel(platform="PCF", stain="DAPI")]
    assert not names & {c.name for c in channels}


def test_a_channel_is_a_claim_subject():
    channel = ImagingChannel(platform="PCF", antibody=NAKATPASE_ANTIBODY)
    store = MemoryClaimStore(MARKER_DOMAIN,
                             subject_types={channel.name: MARKER_TYPES.IMAGING_CHANNEL})
    claim = MarkerClaim(subject=channel.name, predicate=MarkerPredicate.SEGMENTATION_ROLE,
                        value="cyto_structural", source="uniprot", source_id="P05023",
                        evidence="Cell membrane; Multi-pass membrane protein.")
    store.add(claim)
    assert store.held(channel.name) == [claim]
    assert store.in_state("unresolved") == [channel.name]
