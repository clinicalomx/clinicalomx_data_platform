"""This repo's lamindb vocabulary: the types, features and ontology pins it declares.

Published through the ``clinicalomx.vocabulary`` entry point. ``VOCAB.ensure()`` creates
everything on the instance.

Names are imported from the modules that own them. Inside the package, import from the owner,
not from here: this module imports the whole chain. Importing it must not pull in lamindb,
bionty, django or psycopg2; `tests/test_boundaries.py` checks this.

Predicates are not declared: a domain adds one without a schema change.
"""

from __future__ import annotations

from clinicalomx.vocab import Vocabulary
from clinicalomx_ledger.grammar.gate import ReviewState
from clinicalomx_ledger.grammar.model import DecisionKind, Outcome
from clinicalomx_reagents.inventory import ANTIBODY_BARCODE_PREFIX, REPORTER_BARCODE_PREFIX
from clinicalomx_reagents.spec import NO_AKOYA_ID, SeedStatus

from .domain.imaging_channel import CHANNEL_FEATURES, PLATFORM_SOURCE
from .domain.marker import (
    MARKER_DOMAIN,
    MARKER_FEATURES,
    MARKER_TYPES,
    MarkerPredicate,
    MarkerSource,
)
from .domain.reagent_mapping import (
    MAPPING_FEATURES,
    MAPPING_TYPES,
    REAGENT_MAPPING_DOMAIN,
    ReagentMappingPredicate,
    ReagentMappingSource,
)
from .seed_spec import SegmentationRole, TargetKind
from .sources.snapshot import SNAPSHOT_FEATURES

# --- the names --------------------------------------------------------------------------


class MODEL_FEATURES:
    """The 23 retired claim, verdict and invalidation feature names.

    Nothing writes these. They stay declared on the instance.
    """

    # --- Claim --------------------------------------------------------------
    #: What the claim is about, as a Record link.
    CLAIM_SUBJECT = "claim_subject"
    CLAIM_PREDICATE = "claim_predicate"
    CLAIM_VALUE = "claim_value"
    #: A source vocabulary member: uniprot, pubmed, url or human.
    CLAIM_SOURCE = "claim_source"
    #: Accession, PMID or URL.
    CLAIM_SOURCE_ID = "claim_source_id"
    CLAIM_SOURCE_VERSION = "claim_source_version"
    #: Verbatim upstream text.
    CLAIM_EVIDENCE = "claim_evidence"

    # --- Verdict ------------------------------------------------------------
    #: What the decision is about, as a Record link.
    VERDICT_SUBJECT = "verdict_subject"
    VERDICT_PREDICATE = "verdict_predicate"
    VERDICT_VALUE = "verdict_value"
    #: The supporting claims, as Record links.
    VERDICT_SUPPORTED_BY = "verdict_supported_by"
    #: The status before the decision, blank when none.
    VERDICT_FROM_STATUS = "verdict_from_status"
    VERDICT_TO_STATUS = "verdict_to_status"
    #: rule | hint | one_off
    VERDICT_KIND = "verdict_kind"
    VERDICT_REASON = "verdict_reason"
    VERDICT_BY = "verdict_by"
    VERDICT_AT = "verdict_at"

    # --- SourceInvalidation -------------------------------------------------
    #: The source vocabulary member being voided, and the id under it.
    INVALIDATION_SOURCE = "invalidation_source"
    INVALIDATION_SOURCE_ID = "invalidation_source_id"
    #: Blank voids every release.
    INVALIDATION_SOURCE_VERSION = "invalidation_source_version"
    INVALIDATION_REASON = "invalidation_reason"
    INVALIDATION_BY = "invalidation_by"
    INVALIDATION_AT = "invalidation_at"


class TYPES(MARKER_TYPES, MAPPING_TYPES):
    """``ln.Record`` type records owned by this repo, inherited from the owning domains.

    Claim, verdict and invalidation types carry their domain in the attribute name, so two
    domains never shadow each other. The two below have no owning module.
    """

    #: Umbrella for how an analyte was measured.
    MEASUREMENT = "Measurement"
    #: Placeholder until an assay arrives.
    BLOOD_PROTEOMIC_ASSAY = "BloodProteomicAssay"


class FEATURES(MODEL_FEATURES, MARKER_FEATURES, MAPPING_FEATURES, SNAPSHOT_FEATURES,
               CHANNEL_FEATURES):
    """``ln.Feature`` names owned by this repo, inherited from the owning modules. Disjoint."""


#: Provenance a claim must carry to be citable, as feature names. The field-level twin is
#: `clinicalomx_ledger.grammar.model.REQUIRED`.
REQUIRED_PROVENANCE = (
    FEATURES.CLAIM_SOURCE,
    FEATURES.CLAIM_SOURCE_ID,
    FEATURES.CLAIM_EVIDENCE,
)


VOCAB = Vocabulary("clinicalomx_markers")

# --- WHAT MAY BE DECLARED HERE ----------------------------------------------------------
# A value that can be disputed is a CLAIM. Structural identity is a FIELD. A structural value
# that can be derived is not stored at all. A new feature states which side it is on.

# --- entities ---------------------------------------------------------------------------
MARKER = VOCAB.type(TYPES.MARKER, "A canonical analyte, named once per lab.")
MEASUREMENT = VOCAB.type(TYPES.MEASUREMENT, "How an analyte was measured.")
# Parentless: keyed on its reagent construct plus its platform.
IMAGING_CHANNEL = VOCAB.type(
    TYPES.IMAGING_CHANNEL,
    "A channel in an image: PCF, or the morphology channels of a CosMx or GeoMx run.",
)

# `TYPES.ARTIFACT` is not declared: lamindb already owns the Artifact registry.

# --- claim and verdict rows, one type per domain ----------------------------------------
# Claims share every feature across domains; the type keeps each domain's rows apart.
MARKER_CLAIM = VOCAB.type(
    TYPES.MARKER_CLAIM,
    "One assertion about a Marker or an ImagingChannel, with provenance.",
)
MAPPING_CLAIM = VOCAB.type(
    TYPES.MAPPING_CLAIM,
    "One assertion that a spreadsheet column fills a reagent field, with the evidence.",
)

# --- Verdict ------------------------------------------------------------------------------
# Parentless: a verdict is not a kind of claim.
MARKER_VERDICT = VOCAB.type(
    TYPES.MARKER_VERDICT,
    "One decision about a Marker or an ImagingChannel, append-only: what stands, which "
    "transition it made, and the human behind it.",
)
MAPPING_VERDICT = VOCAB.type(
    TYPES.MAPPING_VERDICT,
    "One decision about which spreadsheet column fills a reagent field, append-only.",
)

# --- SourceInvalidation -------------------------------------------------------------------
# One type per domain: both source vocabularies spell `human`. An invalidation voids a source
# without deleting its claims; reads stop standing them.
MARKER_SOURCE_INVALIDATION = VOCAB.type(
    TYPES.MARKER_SOURCE_INVALIDATION,
    "One source voided: every claim about a Marker or an ImagingChannel read from it stops "
    "standing, append-only.",
)
MAPPING_SOURCE_INVALIDATION = VOCAB.type(
    TYPES.MAPPING_SOURCE_INVALIDATION,
    "One source voided: every column mapping read from it stops standing, append-only.",
)

# --- ImagingChannel identity ------------------------------------------------------------
# Half of a channel's key: the same kit on two assays is two channels. An EFO term.
PLATFORM = VOCAB.feature(
    FEATURES.PLATFORM, f"cat[bionty.{PLATFORM_SOURCE['entity']}]",
    "Acquisition platform, as its EFO term, e.g. PhenoCycler-Fusion for PCF.")

# --- Claim ------------------------------------------------------------------------------
# Source vocabularies and predicate sets are read from the types that govern them.
CLAIM_SUBJECT = VOCAB.feature(
    FEATURES.CLAIM_SUBJECT, "cat[Record]",
    "What this claim is about; see the domain's ClaimDomain.subject_types.")
CLAIM_PREDICATE = VOCAB.feature(
    FEATURES.CLAIM_PREDICATE, str,
    f"What the claim asserts: {', '.join(MarkerPredicate)} for a marker, "
    f"{', '.join(ReagentMappingPredicate)} for a column mapping.")
CLAIM_VALUE = VOCAB.feature(FEATURES.CLAIM_VALUE, str, "The asserted value.")
CLAIM_SOURCE = VOCAB.feature(
    FEATURES.CLAIM_SOURCE, str,
    f"Where it came from: {', '.join(MarkerSource)} for a marker, "
    f"{', '.join(ReagentMappingSource)} for a column mapping.")
CLAIM_SOURCE_ID = VOCAB.feature(
    FEATURES.CLAIM_SOURCE_ID, str, "Accession, PMID or URL a citation resolves to upstream.")
CLAIM_SOURCE_VERSION = VOCAB.feature(
    FEATURES.CLAIM_SOURCE_VERSION, str, "Upstream release the value was read from.")
CLAIM_EVIDENCE = VOCAB.feature(
    FEATURES.CLAIM_EVIDENCE, str, "Verbatim upstream text, so a citation can quote it.")

# --- Verdict fields -----------------------------------------------------------------------
VERDICT_SUBJECT = VOCAB.feature(
    FEATURES.VERDICT_SUBJECT, "cat[Record]", "What this decision is about.")
VERDICT_PREDICATE = VOCAB.feature(
    FEATURES.VERDICT_PREDICATE, str, "What was decided about the subject.")
VERDICT_VALUE = VOCAB.feature(
    FEATURES.VERDICT_VALUE, str, "The value the lab has settled on.")
VERDICT_SUPPORTED_BY = VOCAB.feature(
    FEATURES.VERDICT_SUPPORTED_BY, "cat[Record]", "The claims this decision rests on.")
VERDICT_FROM_STATUS = VOCAB.feature(
    FEATURES.VERDICT_FROM_STATUS, str,
    "What stood before the decision; blank when nothing stood.")
VERDICT_TO_STATUS = VOCAB.feature(
    FEATURES.VERDICT_TO_STATUS, str,
    # Retired: keeps the three statuses it was written under.
    "What stands after it, and so the subject's state: proposed, approved, rejected.")
VERDICT_KIND = VOCAB.feature(
    FEATURES.VERDICT_KIND, str,
    f"How a rejection generalises: {', '.join(DecisionKind)}.")
VERDICT_REASON = VOCAB.feature(
    FEATURES.VERDICT_REASON, str, "Why the human accepted or rejected it.")
VERDICT_BY = VOCAB.feature(FEATURES.VERDICT_BY, str, "Who gave the verdict.")
VERDICT_AT = VOCAB.feature(FEATURES.VERDICT_AT, str, "When, ISO date.")

# --- SourceInvalidation fields ------------------------------------------------------------
# Separate from the claim's source pair. `by`, `at` and `reason` are mandatory.
INVALIDATION_SOURCE = VOCAB.feature(
    FEATURES.INVALIDATION_SOURCE, str, "The source whose claims are void.")
INVALIDATION_SOURCE_ID = VOCAB.feature(
    FEATURES.INVALIDATION_SOURCE_ID, str,
    "Accession, PMID, URL or person the void applies to.")
INVALIDATION_SOURCE_VERSION = VOCAB.feature(
    FEATURES.INVALIDATION_SOURCE_VERSION, str,
    "Upstream release voided; blank voids every release.")
INVALIDATION_REASON = VOCAB.feature(
    FEATURES.INVALIDATION_REASON, str, "Why the source is void.")
INVALIDATION_BY = VOCAB.feature(FEATURES.INVALIDATION_BY, str, "Who voided it.")
INVALIDATION_AT = VOCAB.feature(FEATURES.INVALIDATION_AT, str, "When, ISO date.")

# --- snapshot artifact ------------------------------------------------------------------
CLAIM_RETRIEVED_AT = VOCAB.feature(
    FEATURES.CLAIM_RETRIEVED_AT, str,
    "When a snapshot was fetched, ISO date. On the snapshot artifact, not on a claim.")

# --- review workflow --------------------------------------------------------------------
# One review state feature per domain.
MARKER_STATE = VOCAB.feature(
    FEATURES.MARKER_STATE, str, f"Marker review state: {', '.join(ReviewState)}.")
MAPPING_STATE = VOCAB.feature(
    FEATURES.MAPPING_STATE, str, f"Column-mapping review state: {', '.join(ReviewState)}.")

# Review notes are a `Decision`'s `reason`, not a feature.

# Identity comes from UniProt, pinned. bt.CellMarker is unused.
VOCAB.source("Protein", source="uniprot", version="2024-03", organism="human")

# Platform comes from EFO, pinned. This pin relinks existing terms.
VOCAB.source(PLATFORM_SOURCE["entity"], source=PLATFORM_SOURCE["source"],
             version=PLATFORM_SOURCE["version"], relink=True)

__all__ = [
    "ANTIBODY_BARCODE_PREFIX",
    "FEATURES",
    "MARKER_DOMAIN",
    "NO_AKOYA_ID",
    "REAGENT_MAPPING_DOMAIN",
    "REPORTER_BARCODE_PREFIX",
    "REQUIRED_PROVENANCE",
    "TYPES",
    "VOCAB",
    "DecisionKind",
    "MarkerPredicate",
    "MarkerSource",
    "Outcome",
    "ReagentMappingPredicate",
    "ReagentMappingSource",
    "ReviewState",
    "SeedStatus",
    "SegmentationRole",
    "TargetKind",
]
