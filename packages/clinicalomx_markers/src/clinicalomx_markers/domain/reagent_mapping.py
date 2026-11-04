"""The spreadsheet-mapping domain: which column of a vendor sheet fills which reagent field.

A human approves each mapping before anything is loaded::

    subject   = the spreadsheet          an Artifact, which is a record
    predicate = rx_barcode               the field being populated
    value     = "Sheet1!C1"              the column it comes from, and how
    evidence  = header 'Reporter Barcode'; 28/28 values match RX[0-9]{3}

The value is a `transform.Reading`, such as ``Sheet1!B1 | between("(", ")", last=True)``.
One cell holding a target, a clone and a barcode is three claims, each reviewed alone.
"""

from __future__ import annotations

from clinicalomx_ledger.grammar.model import (
    Claim,
    ClaimDomain,
    ClaimPredicate,
    ClaimSource,
    Decision,
    SourceInvalidation,
)

from . import transform
from .rules import check as rules_check


class MAPPING_TYPES:
    """``ln.Record`` type records this domain names."""

    #: The spreadsheet itself, as a registered ``ln.Artifact``.
    ARTIFACT = "Artifact"

    #: One assertion that a column fills a field, with the evidence for it.
    MAPPING_CLAIM = "MappingClaim"

    #: One decision about which column fills a field.
    MAPPING_VERDICT = "MappingVerdict"

    #: One record voiding a source, such as every mapping read from one sheet release.
    MAPPING_SOURCE_INVALIDATION = "MappingSourceInvalidation"


class MAPPING_FEATURES:
    """``ln.Feature`` names this domain owns: only the review state."""

    #: Drives this domain's review queue, separate from ``marker_state``.
    MAPPING_STATE = "mapping_state"


class ReagentMappingSource(ClaimSource):
    """Where a claim about a column mapping came from: the sheet, or a qptiff's metadata."""

    LLM = "llm"
    HUMAN = "human"
    SPREADSHEET = "spreadsheet"
    IMAGE_METADATA = "image_metadata"


class ReagentMappingPredicate(ClaimPredicate):
    """The reagent fields a spreadsheet column can fill, spelled as the seed columns."""

    RX_BARCODE = "rx_barcode"
    BX_BARCODE = "bx_barcode"
    AKOYA_ID = "akoya_id"
    FLUOROPHORE = "fluorophore"
    CATALOGUE_NUMBER = "catalogue_number"
    TARGET_LABEL = "target_label"
    #: The antibody clone. No seed CSV holds it yet.
    CLONE = "clone"


class MappingClaim(Claim[ReagentMappingSource, ReagentMappingPredicate], entity="Spreadsheet"):
    """One assertion that a column of a sheet fills a reagent field.

    >>> mapped = MappingClaim(
    ...     subject="akoya-panel-2026-03.xlsx",
    ...     predicate=ReagentMappingPredicate.RX_BARCODE,
    ...     value="Reporter Barcode",
    ...     source="spreadsheet",
    ...     source_id="sheet1!C1",
    ...     evidence="header 'Reporter Barcode'; 28/28 values match RX[0-9]{3}",
    ... )
    >>> mapped.claim_id()
    'akoya-panel-2026-03.xlsx:rx_barcode:spreadsheet:sheet1!C1:3d36959d789f'

    The descriptions substitute this domain's entity:

    >>> MappingClaim.model_fields["subject"].description
    'The Spreadsheet this claim is about.'
    """


class MappingDecision(Decision[ReagentMappingPredicate], entity="Spreadsheet"):
    """One ruling on a mapping claim. See `clinicalomx_ledger.grammar.model.Decision`."""


class MappingSourceInvalidation(SourceInvalidation[ReagentMappingSource],
                                entity="Spreadsheet"):
    """One source voided. See `clinicalomx_ledger.grammar.model.SourceInvalidation`."""


#: A claim is about the sheet, an ``ln.Artifact``.
SUBJECT_TYPES = (MAPPING_TYPES.ARTIFACT,)

#: Every predicate is reviewed by a human.
REVIEWED_PREDICATES = tuple(ReagentMappingPredicate)

def check(predicate: str, value: str, subject_row: dict[str, str]) -> None:
    """The rejection ratchet, plus: a value containing ``|`` must parse as a transform."""
    rules_check(predicate, value, subject_row)
    if "|" in value:
        transform.parse(value)


class NeedsAHuman(ValueError):
    """An agent tried to accept what only a human may. `api.v1` answers it as a 403."""

    #: The rule a refusal names.
    rule = "a_bespoke_transform_is_accepted_by_a_human"


def agent_may_accept(predicate: str, value: str, subject_row: dict[str, str]) -> None:
    """Refuse an agent accepting a bespoke transform, whatever its confidence or policy.

    A library chain, or a value that is no reading, passes.
    """
    try:
        reading = transform.parse(value)
    except ValueError:
        return
    if reading.bespoke:
        raise NeedsAHuman(
            f"{predicate} = {value}: the transform holds newly written code, and only a human "
            "accepts one; no policy lets an agent accept it")


#: The mapping domain, for a claim store.
REAGENT_MAPPING_DOMAIN = ClaimDomain(
    name="reagent_mapping",
    claim=MappingClaim,
    decision=MappingDecision,
    invalidation=MappingSourceInvalidation,
    subject_types=SUBJECT_TYPES,
    reviewed_predicates=REVIEWED_PREDICATES,
    claim_type=MAPPING_TYPES.MAPPING_CLAIM,
    verdict_type=MAPPING_TYPES.MAPPING_VERDICT,
    invalidation_type=MAPPING_TYPES.MAPPING_SOURCE_INVALIDATION,
    state_feature=MAPPING_FEATURES.MAPPING_STATE,
    policy=check,
    agent_may_accept=agent_may_accept,
)
