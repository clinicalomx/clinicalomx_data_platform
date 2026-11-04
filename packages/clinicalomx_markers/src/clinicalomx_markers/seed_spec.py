"""What each of this package's seed CSVs may contain, declared once.

Covers the five files in ``seed_data/``; ``seed_data/README.md`` is generated from it. The
reagent inventory is declared in ``clinicalomx_reagents.spec``. Vocabularies are ``StrEnum``
types, which compare equal to their strings. Cross-row rules do not belong here.
"""

from __future__ import annotations

from enum import StrEnum

from clinicalomx_reagents.spec import STATUS, UNIPROT_ACCESSION, Column, SeedFile


class SegmentationRole(StrEnum):
    """Values of the `segmentation_role` claim predicate.

    ``MARKER`` excludes the channel from segmentation; ``NUCLEUS`` feeds nuclear segmentation.
    """

    NUCLEUS = "nucleus"
    CYTO_STRUCTURAL = "cyto_structural"
    MARKER = "marker"
    EXCLUDE = "exclude"


class TargetKind(StrEnum):
    """What a target claim's candidate set means. `domain.target.combination` says how each
    kind combines localisations."""

    #: One protein, isoform not distinguished.
    SINGLE = "single"
    #: One of these, and nobody can say which.
    ISOFORM_AMBIGUOUS = "isoform_ambiguous"
    #: Binds all of them at once: panCK across the keratins.
    CROSS_REACTIVE = "cross_reactive"
    #: Part of a complex, whose members are the candidates.
    COMPLEX_MEMBER = "complex_member"


#: One candidate of a target claim: a gene product, or one isoform of it.
UNIPROT_CANDIDATE = rf"(?:{UNIPROT_ACCESSION})(?:-[0-9]+)?"

#: What joins the candidates of a target claim, in its value and in ``targets.csv``.
CANDIDATE_SEPARATOR = "|"

#: The pinned Gene Ontology release, spelled as bionty spells it. Never ``latest``.
GO_RELEASE = "2025-10-10"

#: A GO identifier, as GO publishes it.
GO_ID = r"GO:[0-9]{7}"


MARKERS = SeedFile(
    "markers.csv",
    "Canonical analytes: name, full name, status",
    (
        Column("canonical_name", "The lab's canonical analyte name", key=True, blank_ok=False),
        Column("full_name", "The long name, so a reviewer reading a diff knows what this is"),
        STATUS,
    ),
)

TARGETS = SeedFile(
    "targets.csv",
    "Seed target claims: the candidate proteins a marker binds, and their kind",
    (
        Column("canonical_name", "The marker the claim is about", key=True, blank_ok=False),
        Column("kind", "How the candidates relate, which decides how localisations combine",
               blank_ok=False, one_of=tuple(TargetKind)),
        Column("candidates", "UniProt accessions or IsoIds, pipe separated. Every one is kept",
               blank_ok=False,
               pattern=rf"{UNIPROT_CANDIDATE}(?:\{CANDIDATE_SEPARATOR}{UNIPROT_CANDIDATE})*",
               pattern_says="UniProt accessions or IsoIds, pipe separated"),
        Column("evidence", "What the claim quotes, so a citation has text to show",
               blank_ok=False),
        Column("curated_by", "Who stated it. The claim's source_id", blank_ok=False),
    ),
)

CHANNELS = SeedFile(
    "channels.csv",
    "(marker, platform) to imaging role, plus compartment evidence",
    (
        Column("canonical_name", "The marker this channel measures", key=True, blank_ok=False),
        Column("platform", "The instrument the channel was acquired on", blank_ok=False),
        Column("segmentation_role", "What the channel is good for in segmentation",
               blank_ok=False, one_of=tuple(SegmentationRole)),
        Column("compartment", "Localisation terms, pipe separated. rules.csv matches on it"),
        Column("go_cc", "GO cellular component id, where one was recorded"),
        Column("uniprot_cc_raw", "The verbatim UniProt subcellular location text"),
        Column("uniprot_release", "Which UniProt release the evidence was read from"),
        Column("role_source", "Who or what decided the role"),
        STATUS,
    ),
)

STRUCTURES = SeedFile(
    "structures.csv",
    "Curated UniProt localisation prose to the GO cellular component it names",
    (
        Column("uniprot_location", "The UniProt subcellular location term, verbatim",
               key=True, blank_ok=False),
        Column("go_id", "The GO cellular component the term names", blank_ok=False,
               pattern=GO_ID, pattern_says="a GO identifier such as GO:0005737"),
        Column("go_name", "The GO term's name at that release, for a reviewer to read",
               blank_ok=False),
        Column("go_release", "The GO release the row was checked against", blank_ok=False,
               one_of=(GO_RELEASE,)),
        Column("curated_by", "Who made the mapping", blank_ok=False),
    ),
)

RULES = SeedFile(
    "rules.csv",
    "Constraints promoted from rejections, enforced by domain.rules",
    (
        Column("predicate", "Which claim predicate the rule constrains", key=True,
               blank_ok=False),
        Column("when_field", "A field on the subject to test"),
        Column("when_contains", "Substring match against that field, case insensitive"),
        Column("forbid_value", "The value that must not be proposed"),
        Column("reason", "Why, in the words of whoever rejected it"),
        Column("added_by", "Who promoted the rejection"),
    ),
)

SEED_FILES: dict[str, SeedFile] = {
    spec.filename: spec
    for spec in (MARKERS, TARGETS, CHANNELS, STRUCTURES, RULES)
}

