from __future__ import annotations

from clinicalomx_ledger.grammar.model import (
    Claim,
    ClaimDomain,
    ClaimPredicate,
    ClaimSource,
    Decision,
    SourceInvalidation,
)

from .rules import check as rules_check


class MARKER_TYPES:
    """``ln.Record`` type records the marker domain owns."""

    #: The lab's canonical analyte.
    MARKER = "Marker"

    #: A channel in an image: PCF, or a CosMx or GeoMx morphology channel.
    IMAGING_CHANNEL = "ImagingChannel"

    #: One citable assertion about a subject in :data:`SUBJECT_TYPES`, with its provenance.
    MARKER_CLAIM = "MarkerClaim"

    #: One decision on a claim, and the human behind it.
    MARKER_VERDICT = "MarkerVerdict"

    #: One record voiding a source, so its claims stop standing.
    MARKER_SOURCE_INVALIDATION = "MarkerSourceInvalidation"


class MARKER_FEATURES:
    """``ln.Feature`` names this domain owns: only the review state."""

    #: The marker review state, used through clinicalomx.review. Carried by any claim subject.
    MARKER_STATE = "marker_state"


class MarkerSource(ClaimSource):
    """Where a claim about a marker came from."""

    UNIPROT = "uniprot"
    PUBMED = "pubmed"
    URL = "url"
    HUMAN = "human"


class MarkerPredicate(ClaimPredicate):
    """What a claim about a marker or a channel can assert.

    Values are part of `Claim.claim_id`: renaming one orphans every citation written under it.
    """

    SUBCELLULAR_LOCATION = "subcellular_location"
    GO_CC = "go_cc"
    FUNCTION = "function"
    CELL_TYPING_UTILITY = "cell_typing_utility"
    SEGMENTATION_ROLE = "segmentation_role"
    #: Which proteins a marker binds. `domain.target` owns the value.
    TARGET = "target"
    #: Another spelling of the marker. One source may list several. `propose.alias` proposes it.
    ALIAS = "alias"


class MarkerClaim(Claim[MarkerSource, MarkerPredicate], entity="Marker"):
    """One assertion about a Marker or an ImagingChannel.

    See `clinicalomx_ledger.grammar.model.Claim`.

    >>> MarkerClaim.model_fields["subject"].description
    'The Marker this claim is about.'
    >>> MarkerClaim(subject="CD8A", predicate="segmentation_role", value="marker",
    ...             source="uniprot", source_id="P01732", evidence="Cell membrane.").source
    <MarkerSource.UNIPROT: 'uniprot'>
    """


class MarkerDecision(Decision[MarkerPredicate], entity="Marker"):
    """One ruling on a marker claim. See `clinicalomx_ledger.grammar.model.Decision`."""


class MarkerSourceInvalidation(SourceInvalidation[MarkerSource], entity="Marker"):
    """One source voided. See `clinicalomx_ledger.grammar.model.SourceInvalidation`."""


#: What a claim may be about: a Marker for the biology, an ImagingChannel for what follows
#: from the measurement, such as ``segmentation_role``.
SUBJECT_TYPES = (MARKER_TYPES.MARKER, MARKER_TYPES.IMAGING_CHANNEL)

#: Predicates a human reviews. Only these move a subject through the queue.
REVIEWED_PREDICATES = (MarkerPredicate.SEGMENTATION_ROLE, MarkerPredicate.TARGET,
                       MarkerPredicate.ALIAS)

#: Predicates one source holds several values of at once.
MULTI_VALUED_PREDICATES = (MarkerPredicate.ALIAS,)

#: The marker domain, for a claim store.
MARKER_DOMAIN = ClaimDomain(
    name="marker",
    claim=MarkerClaim,
    decision=MarkerDecision,
    invalidation=MarkerSourceInvalidation,
    subject_types=SUBJECT_TYPES,
    reviewed_predicates=REVIEWED_PREDICATES,
    multi_valued_predicates=MULTI_VALUED_PREDICATES,
    claim_type=MARKER_TYPES.MARKER_CLAIM,
    verdict_type=MARKER_TYPES.MARKER_VERDICT,
    invalidation_type=MARKER_TYPES.MARKER_SOURCE_INVALIDATION,
    state_feature=MARKER_FEATURES.MARKER_STATE,
    policy=rules_check,
)
