"""The smallest complete domain declaration, used by the doctests and the curation recipe."""

from __future__ import annotations

from .model import (
    Claim,
    ClaimDomain,
    ClaimPredicate,
    ClaimSource,
    Decision,
    SourceInvalidation,
)


class SampleSource(ClaimSource):
    """Where a claim about a sample came from."""

    SOP = "sop"
    HUMAN = "human"


class SamplePredicate(ClaimPredicate):
    """What a claim about a sample can assert.

    The string is part of `Claim.claim_id`; renaming one orphans every citation under it.
    """

    DISPOSITION = "disposition"


class SampleClaim(Claim[SampleSource, SamplePredicate], entity="Sample"):
    """One assertion about a Sample. See `model.Claim`."""


class SampleDecision(Decision[SamplePredicate], entity="Sample"):
    """One ruling on a claim about a Sample. See `model.Decision`."""


class SampleSourceInvalidation(SourceInvalidation[SampleSource], entity="Sample"):
    """One source voided in this domain. See `model.SourceInvalidation`."""


#: The whole declaration, with the default `policy`.
EXAMPLE_DOMAIN = ClaimDomain(
    name="example",
    claim=SampleClaim,
    decision=SampleDecision,
    invalidation=SampleSourceInvalidation,
    subject_types=("Sample",),
    reviewed_predicates=(SamplePredicate.DISPOSITION,),
    claim_type="SampleClaim",
    verdict_type="SampleVerdict",
    invalidation_type="SampleSourceInvalidation",
    state_feature="sample_review_state",
)
