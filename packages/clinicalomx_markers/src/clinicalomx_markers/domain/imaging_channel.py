"""An ImagingChannel's identity: its reagent construct plus its platform. No model.

The reagent is an antibody (its reporter follows from its BX barcode) or a fluorophore used
alone as a stain, such as DAPI. The same reagent on two platforms is two channels.

The marker is derived from the reagent and never passed in: an antibody's inventory row
names it, and a stain is its own marker. The name joins reagent and platform with ``@``, which
no marker name contains.

A proximity-ligation channel is a `PLAChannel`: a pair of probes measuring a
`ProteinInteraction`, with no single marker.

Imports no lamindb.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clinicalomx_reagents.inventory import reporter_for
from clinicalomx_reagents.rows import antibodies as _antibody_rows
from clinicalomx_reagents.rows import fluorophores as _fluorophore_rows

from ..seed_data.rows import markers as _marker_rows
from ..seed_spec import TargetKind
from .interaction import ProteinInteraction
from .target import TargetSet, seed_claims


class CHANNEL_FEATURES:
    """``ln.Feature`` names an ImagingChannel record carries."""

    #: Half of a channel's key. Its value is the EFO term the platform label resolves to under
    #: `PLATFORM_SOURCE`, never the label itself.
    PLATFORM = "platform"


#: The pinned ontology a channel's platform resolves through. EFO 3.85.0 has no CosMx term.
PLATFORM_SOURCE = {"entity": "ExperimentalFactor", "source": "efo", "version": "3.85.0"}

#: Each platform label a channel key may carry, and its EFO term at `PLATFORM_SOURCE`.
PLATFORM_TERMS = {
    "PCF": "EFO:0700001",  # PhenoCycler-Fusion
    "GeoMx": "EFO:0030029",  # NanoString digital spatial profiling
}


#: Joins a channel's reagent to its platform in its name.
PLATFORM_SEPARATOR = "@"

#: Joins a PLA channel's two probes in its name.
PROBE_SEPARATOR = "&"

#: Joins an antibody to its reporter in a channel's name.
REPORTER_SEPARATOR = "+"


def _platform(value: str | None) -> str:
    platform = (value or "").strip()
    if not platform:
        raise ValueError("a channel names its platform: without one it is not a channel")
    if PLATFORM_SEPARATOR in platform:
        raise ValueError(f"platform {platform!r} may not contain {PLATFORM_SEPARATOR!r}")
    return platform


def _row(rows: list[dict[str, str]], column: str, key: str) -> dict[str, str] | None:
    return next((r for r in rows if (r.get(column) or "").strip() == key), None)


@dataclass(frozen=True)
class ImagingChannel:
    """One channel definition, built from its key and nothing else.

    Pass ``antibody`` (a catalogue number) or ``stain`` (a fluorophore's canonical name), never
    both, plus the ``platform``. Anything the registry cannot resolve raises.

    >>> ch = ImagingChannel(platform="PCF", antibody="4350012")
    >>> ch.name, ch.reporter, ch.fluorophore, ch.marker
    ('4350012+RX002@PCF', 'RX002', 'ALEXA_FLUOR_647', 'NAKATPASE')
    >>> ImagingChannel(platform="PCF", stain="DAPI").marker
    'DAPI'
    >>> ImagingChannel(platform="PCF", antibody="4350012", marker="VIMENTIN")
    Traceback (most recent call last):
    TypeError: ImagingChannel.__init__() got an unexpected keyword argument 'marker'
    """

    platform: str
    antibody: str | None = None
    stain: str | None = None

    # Derived from the reagent: not settable, and not part of equality.
    reporter: str | None = field(init=False, compare=False, default=None)
    fluorophore: str = field(init=False, compare=False, default="")
    marker: str = field(init=False, compare=False, default="")

    def __post_init__(self) -> None:
        platform = _platform(self.platform)
        antibody = (self.antibody or "").strip() or None
        stain = (self.stain or "").strip() or None
        if (antibody is None) == (stain is None):
            raise ValueError("a channel's reagent is an antibody or a direct stain, "
                             "so name exactly one of the two")
        derived = self._from_antibody(antibody) if antibody else self._from_stain(stain)
        for name, value in (("platform", platform), ("antibody", antibody), ("stain", stain),
                            *derived.items()):
            object.__setattr__(self, name, value)

    @staticmethod
    def _from_antibody(catalogue_number: str) -> dict[str, str]:
        row = _row(_antibody_rows(), "catalogue_number", catalogue_number)
        if row is None:
            raise ValueError(f"no antibody with catalogue_number {catalogue_number!r}")
        reporter = reporter_for(row["bx_barcode"])
        if reporter is None:
            raise ValueError(f"antibody {catalogue_number!r} ({row['bx_barcode']}) has no "
                             "reporter stocked, so it is not yet a construct")
        return {"reporter": reporter["rx_barcode"].strip(),
                "fluorophore": reporter["fluorophore"].strip(),
                "marker": row["canonical_name"].strip()}

    @staticmethod
    def _from_stain(fluorophore: str) -> dict[str, str]:
        if _row(_fluorophore_rows(), "canonical_name", fluorophore) is None:
            raise ValueError(f"no fluorophore named {fluorophore!r}")
        if _row(_marker_rows(), "canonical_name", fluorophore) is None:
            raise ValueError(f"fluorophore {fluorophore!r} is not a marker, so used alone it "
                             "measures nothing: only a dye that is also a Marker is a stain")
        return {"reporter": None, "fluorophore": fluorophore, "marker": fluorophore}

    @property
    def name(self) -> str:
        """The key written out: the record name and the claim subject."""
        reagent = (f"{self.antibody}{REPORTER_SEPARATOR}{self.reporter}"
                   if self.antibody else self.stain)
        return f"{reagent}{PLATFORM_SEPARATOR}{self.platform}"


@dataclass(frozen=True)
class PLAChannel:
    """A proximity-ligation channel: a pair of antibody probes plus its platform.

    Its subject is the `ProteinInteraction` of the probes' targets. Reading ``marker`` raises.
    Each probe's marker must have a ``single`` seed target claim. The pair is unordered.

    >>> pla = PLAChannel(platform="PCF", probes=("4350012", "4350003"))
    >>> pla.name, pla.interaction.members
    ('4350003&4350012@PCF', ('P05023', 'P12830'))
    >>> pla.marker
    Traceback (most recent call last):
    TypeError: 4350003&4350012@PCF measures the interaction P05023&P12830, not one marker
    """

    platform: str
    probes: tuple[str, str]

    interaction: ProteinInteraction = field(init=False, compare=False, default=None)

    def __post_init__(self) -> None:
        platform = _platform(self.platform)
        probes = tuple(sorted((p or "").strip() for p in self.probes))
        if len(probes) != 2 or len(set(probes)) != 2 or not all(probes):
            raise ValueError(f"a PLA channel's reagent is two distinct antibody probes, not "
                             f"{self.probes!r}")
        members = tuple(self._member(probe) for probe in probes)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "probes", probes)
        object.__setattr__(self, "interaction", ProteinInteraction(members))

    @staticmethod
    def _member(catalogue_number: str) -> str:
        row = _row(_antibody_rows(), "catalogue_number", catalogue_number)
        if row is None:
            raise ValueError(f"no antibody with catalogue_number {catalogue_number!r}")
        marker = row["canonical_name"].strip()
        sets = [TargetSet.parse(c.value) for c in seed_claims() if c.subject == marker]
        if len(sets) != 1 or sets[0].kind != TargetKind.SINGLE:
            raise ValueError(f"probe {catalogue_number!r} binds marker {marker!r}, which has no "
                             "single target claim, so it names no one interaction member")
        return sets[0].candidates[0]

    @property
    def marker(self) -> str:
        raise TypeError(f"{self.name} measures the interaction {self.interaction.name}, "
                        "not one marker")

    @property
    def name(self) -> str:
        """The key written out: the record name and the claim subject."""
        return f"{PROBE_SEPARATOR.join(self.probes)}{PLATFORM_SEPARATOR}{self.platform}"
