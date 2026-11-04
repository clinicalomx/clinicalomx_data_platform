"""A PLA channel measures a protein interaction, localised to the overlap of its members via
`domain.target.combine` over a ``complex_member`` set."""

from __future__ import annotations

import dataclasses

import pytest

from clinicalomx_markers.domain.imaging_channel import ImagingChannel, PLAChannel
from clinicalomx_markers.domain.interaction import INTERACTION_KIND, ProteinInteraction
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MARKER_TYPES
from clinicalomx_markers.domain.target import Combine, combination, combine

ECAD, NAK = "P12830", "P05023"
LOCATIONS = {ECAD: ("Cell membrane", "Cytoplasm", "Endosome"), NAK: ("Cell membrane", "Nucleus")}
ECAD_PROBE, NAK_PROBE = "4350003", "4350012"


# --- localisation is the overlap of the members ---------------------------------


def test_an_interaction_localises_to_the_overlap_of_its_members_and_not_their_union():
    interaction = ProteinInteraction((ECAD, NAK))
    union = set(LOCATIONS[ECAD]) | set(LOCATIONS[NAK])
    assert interaction.localisation(LOCATIONS) == ("Cell membrane",)
    assert set(interaction.localisation(LOCATIONS)) != union


def test_members_that_share_no_localisation_give_nothing_rather_than_a_guess():
    apart = {ECAD: ("Cytoplasm",), NAK: ("Nucleus",)}
    assert ProteinInteraction((ECAD, NAK)).localisation(apart) == ()


def test_a_member_with_no_localisation_is_refused_rather_than_dropped():
    with pytest.raises(ValueError, match=NAK):
        ProteinInteraction((ECAD, NAK)).localisation({ECAD: LOCATIONS[ECAD]})


def test_the_overlap_is_the_target_claims_rule_and_not_a_second_one():
    interaction = ProteinInteraction((ECAD, NAK))
    assert combination(INTERACTION_KIND) == Combine.OVERLAP
    assert interaction.localisation(LOCATIONS) == combine(interaction.as_target, LOCATIONS)


# --- what an interaction is --------------------------------------------------------


def test_an_interaction_is_unordered():
    assert ProteinInteraction((ECAD, NAK)) == ProteinInteraction((NAK, ECAD))


@pytest.mark.parametrize("members", [(ECAD,), (ECAD, ECAD), (ECAD, "not-an-accession")],
                         ids=["one", "homodimer", "not an accession"])
def test_an_interaction_names_at_least_two_distinct_proteins(members):
    with pytest.raises(ValueError):
        ProteinInteraction(members)


# --- a PLA channel is not a single-analyte channel ---------------------------------


def test_a_pla_channels_subject_is_the_interaction_of_its_probes_targets():
    pla = PLAChannel(platform="PCF", probes=(NAK_PROBE, ECAD_PROBE))
    assert pla.interaction == ProteinInteraction((ECAD, NAK))
    assert pla.name == f"{ECAD_PROBE}&{NAK_PROBE}@PCF"


def test_a_pla_channel_cannot_be_read_as_a_single_analyte_channel():
    pla = PLAChannel(platform="PCF", probes=(ECAD_PROBE, NAK_PROBE))
    assert not isinstance(pla, ImagingChannel)
    with pytest.raises(TypeError, match="interaction"):
        getattr(pla, "marker", None)


def test_the_interaction_cannot_be_stated():
    with pytest.raises(TypeError, match="interaction"):
        PLAChannel(platform="PCF", probes=(ECAD_PROBE, NAK_PROBE),
                   interaction=ProteinInteraction((ECAD, NAK)))
    pla = PLAChannel(platform="PCF", probes=(ECAD_PROBE, NAK_PROBE))
    assert dataclasses.replace(pla, platform="CosMx").interaction == pla.interaction


def test_a_pla_channel_keys_on_reagent_plus_platform():
    pcf = PLAChannel(platform="PCF", probes=(ECAD_PROBE, NAK_PROBE))
    assert pcf == PLAChannel(platform=" PCF ", probes=(f" {NAK_PROBE}", ECAD_PROBE))
    assert pcf != PLAChannel(platform="CosMx", probes=(ECAD_PROBE, NAK_PROBE))
    singles = {ImagingChannel(platform="PCF", antibody=p).name for p in (ECAD_PROBE, NAK_PROBE)}
    assert pcf.name not in singles


def test_a_pla_channel_is_an_imaging_channel_subject():
    assert MARKER_TYPES.IMAGING_CHANNEL in MARKER_DOMAIN.subject_types


@pytest.mark.parametrize(("probes", "message"), [
    ((ECAD_PROBE,), "two distinct"),
    ((ECAD_PROBE, ECAD_PROBE), "two distinct"),
    ((ECAD_PROBE, "no-such-catalogue"), "no antibody"),
], ids=["one probe", "same probe twice", "unknown probe"])
def test_a_probe_pair_the_registry_cannot_resolve_is_refused(probes, message):
    with pytest.raises(ValueError, match=message):
        PLAChannel(platform="PCF", probes=probes)


def test_a_pla_channel_without_a_platform_is_refused():
    with pytest.raises(ValueError, match="platform"):
        PLAChannel(platform="", probes=(ECAD_PROBE, NAK_PROBE))
