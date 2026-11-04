"""Claims, decisions and source invalidations: the generic curation grammar.

A `Claim` is what a source asserts about a subject. A `Decision` is the lab's ruling on one
claim. A `SourceInvalidation` voids everything one source said. The current answer is each
claim whose newest decision accepts it; an undecided claim is in the review queue.
A domain subclasses the parameterised models; `example.py` is the smallest one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Generic, Literal, NamedTuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Outcome(StrEnum):
    """What a decision rules about one claim. Spelled as the decided `gate.ReviewState` members."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ClaimSource(StrEnum):
    """Where a claim came from. Defined by downstream members."""


class ClaimPredicate(StrEnum):
    """What a claim can assert. Defined by downstream members."""


class DecisionKind(StrEnum):
    """How a rejection should generalise. Blank on an acceptance."""

    RULE = "rule"  # a permanent constraint
    HINT = "hint"  # steering attached to the subject
    ONE_OFF = "one_off"  # corrects only this decision


SourceT = TypeVar("SourceT", bound=ClaimSource)
PredicateT = TypeVar("PredicateT", bound=ClaimPredicate)

#: Hex characters in every digest.
DIGEST_CHARS = 12

#: Longest label before it is shortened: the instance's ``name`` column width.
NAME_LIMIT = 150


def digest(text: str) -> str:
    """Stable short sha256 digest of ``text``."""
    return hashlib.sha256(text.encode()).hexdigest()[:DIGEST_CHARS]


def short_id(claim_id: str, *, limit: int = NAME_LIMIT) -> str:
    """``claim_id`` shortened to ``limit``, its tail replaced by a digest of the whole id.

    >>> short_id("S-001:disposition:sop:SOP-14:0c3d1e4b9a77", limit=30)
    'S-001:disposition:ece2a395be2b'
    """
    if len(claim_id) <= limit:
        return claim_id
    return f"{claim_id[:limit - DIGEST_CHARS - 1]}:{digest(claim_id)}"


class _Stated(BaseModel):
    """Shared base of `Claim` and `Decision`: frozen, closed, descriptions use ``entity``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: ClassVar[str] = "subject"

    def __init_subclass__(cls, entity: str | None = None, **kwargs: Any) -> None:
        """Substitute ``entity`` into field descriptions and rebuild the schema."""
        super().__init_subclass__(**kwargs)
        if entity is None:
            return
        cls.entity = entity
        for field in cls.model_fields.values():
            if field.description:
                field.description = field.description.format(entity=entity)
        cls.model_rebuild(force=True)


class Claim(_Stated, Generic[SourceT, PredicateT]):
    """One assertion a source makes about a subject, with the provenance to cite it.

    A domain subclasses the parameterised model.

    Example:
        >>> from clinicalomx_ledger.grammar.example import (
        ...     SampleClaim, SamplePredicate, SampleSource)
        >>> read = SampleClaim(
        ...     subject="S-001",
        ...     predicate=SamplePredicate.DISPOSITION,
        ...     value="retain",
        ...     source="human",
        ...     source_id="rafael",
        ...     evidence="consent on file, block intact",
        ... )
        >>> read.claim_id()
        'S-001:disposition:human:rafael:4fe6f64e934e'
        >>> read.source is SampleSource.HUMAN and read.source == "human"
        True
        >>> try:
        ...     SampleClaim(subject="S-001", predicate="disposition", value="y",
        ...                 source="wikipedia", source_id="1", evidence="z")
        ... except ValueError as refused:
        ...     print("source" in str(refused))
        True
    """

    subject: Annotated[str, Field(description="The {entity} this claim is about.")]
    predicate: Annotated[
        PredicateT, Field(description="What is being asserted about the {entity}.")]
    value: Annotated[str, Field(description="The asserted value.")]

    source: Annotated[
        SourceT, Field(description="Where this claim about the {entity} was read from.")]
    source_id: Annotated[str, Field(
        description="Accession, PMID, URL or person a citation resolves to upstream.")]

    evidence: Annotated[str, Field(
        description="Verbatim upstream text, so a citation can quote rather than paraphrase.")]
    #: Part of the claim id. Blank for a human assertion.
    source_version: Annotated[str, Field(
        default="", description="Upstream release the value was read at, if it has one.")]
    #: The proposer's own. Not part of the claim id or of repeat detection.
    confidence: Annotated[float | None, Field(
        default=None, ge=0.0, le=1.0,
        description="How sure the proposer is, from 0 to 1, if it said.")]

    def claim_id(self) -> str:
        """Stable id a citation references. The assertion part is digested."""
        return (f"{self.subject}:{self.predicate}:{self.source}:{self.source_id}"
                f":{digest(f'{self.value}@{self.source_version}')}")


class Decision(_Stated, Generic[PredicateT]):
    """One ruling on one claim, append-only. The newest decision on a claim is in effect.

    ``claim_id`` is the decided claim's `short_id`. ``by`` and ``at`` are mandatory.

    Example:
        >>> from clinicalomx_ledger.grammar.example import SampleDecision
        >>> cid = "S-001:disposition:sop:SOP-14:0c3d1e4b9a77"
        >>> decided = SampleDecision(
        ...     subject="S-001", predicate="disposition", value="retain",
        ...     claim_id=cid, outcome="accepted", by="rafael", at="2026-08-24",
        ... )
        >>> (str(decided.outcome), decided.by, decided.at)
        ('accepted', 'rafael', '2026-08-24')

        A decision nobody signed is refused:

        >>> try:
        ...     SampleDecision(subject="S-001", predicate="disposition", value="retain",
        ...                    claim_id=cid, outcome="accepted", by="", at="2026-08-24")
        ... except ValueError as refused:
        ...     print("decision by is blank" in str(refused))
        True
    """

    subject: Annotated[str, Field(description="The {entity} the decided claim is about.")]
    predicate: Annotated[
        PredicateT, Field(description="What the decided claim asserts about the {entity}.")]
    value: Annotated[str, Field(description="The value the decided claim asserts.")]
    claim_id: Annotated[str, Field(description="`short_id` of the claim this rules on.")]

    outcome: Annotated[Outcome, Field(description="Whether the claim is the answer.")]
    kind: Annotated[DecisionKind | Literal[""], Field(
        default="", description="How a rejection generalises. Blank on an acceptance.")]
    reason: Annotated[str, Field(default="", description="Why, in the decider's words.")]
    by: Annotated[str, Field(description="Who decided.")]
    at: Annotated[str, Field(description="When, ISO date.")]

    @model_validator(mode="after")
    def _a_decision_names_its_decider(self) -> Decision:
        for named in ("by", "at"):
            if not str(getattr(self, named)).strip():
                raise ValueError(f"decision {named} is blank: a decision names who made it "
                                 "and when")
        return self

    def decision(self) -> tuple[Any, ...]:
        """The fields that make two decisions the same decision made twice."""
        return (self.claim_id, str(self.outcome), str(self.kind), self.reason, self.by,
                self.at)


class SourceInvalidation(_Stated, Generic[SourceT]):
    """One append-only record voiding everything a source said.

    ``by``, ``at`` and ``reason`` are mandatory and refused blank. Only a human writes one.
    A domain subclasses the parameterised model, so it refuses another domain's source.

    Example:
        >>> from clinicalomx_ledger.grammar.example import (
        ...     SampleSource, SampleSourceInvalidation)
        >>> void = SampleSourceInvalidation(
        ...     source="sop", source_id="SOP-14", source_version="rev-3",
        ...     reason="rev-3 of the SOP shifted the disposition column",
        ...     by="rafael", at="2026-08-27",
        ... )
        >>> void.source is SampleSource.SOP
        True

        Bounded to one release, so a later one is untouched:

        >>> read = Assertion("S-001", "disposition", "sop", "SOP-14")
        >>> void.voids(read, "rev-3"), void.voids(read, "rev-4")
        (True, False)

        Unbounded, every release goes:

        >>> whole = SampleSourceInvalidation(
        ...     source="sop", source_id="SOP-14", reason="wrong SOP cited",
        ...     by="rafael", at="2026-08-27")
        >>> whole.voids(read, "rev-4")
        True

        And a decision nobody signed is refused:

        >>> try:
        ...     SampleSourceInvalidation(source="sop", source_id="SOP-14",
        ...                              reason="wrong SOP cited", by="", at="2026-08-27")
        ... except ValueError as refused:
        ...     print("invalidation by is blank" in str(refused))
        True
    """

    source: Annotated[
        SourceT, Field(description="The source whose claims about a {entity} are void.")]
    source_id: Annotated[str, Field(
        description="Accession, PMID, URL or person the void applies to.")]
    #: Blank voids every release.
    source_version: Annotated[str, Field(
        default="", description="Upstream release voided, or blank for every release.")]

    reason: Annotated[str, Field(description="Why the source is void, in the human's words.")]
    by: Annotated[str, Field(description="Who voided it.")]
    at: Annotated[str, Field(description="When, ISO date.")]

    @model_validator(mode="after")
    def _an_invalidation_names_its_human(self) -> SourceInvalidation:
        for named in ("reason", "by", "at"):
            if not str(getattr(self, named)).strip():
                raise ValueError(
                    f"invalidation {named} is blank: voiding a source is a decision, and a "
                    "decision names the human who made it, when, and why"
                )
        return self

    def voids(self, assertion: Assertion, source_version: str) -> bool:
        """Whether this record voids ``assertion`` as read at ``source_version``.

        A blank ``source_version`` here matches every release; a set one matches exactly.
        """
        return (assertion.source == str(self.source)
                and assertion.source_id == self.source_id
                and self.source_version in ("", source_version))

    def name(self) -> str:
        """Storage name. Not unique: a second invalidation is a second row."""
        return short_id(digest(repr((
            str(self.source), self.source_id, self.source_version,
            self.reason, self.by, self.at,
        ))))


#: A domain's extra check on an assertion: raise to refuse. Takes predicate, value and the
#: subject's row.
Policy = Callable[[str, str, dict[str, str]], None]


def no_policy(predicate: str, value: str, subject_row: dict[str, str]) -> None:
    """Policy that constrains nothing beyond citability. The default."""


class ClaimDomain(BaseModel):
    """Everything the generic machinery needs to know about one domain.

    `docs/curation-recipe.md` explains how to fill it in. Only `policy`,
    `multi_valued_predicates` and `agent_may_accept` have defaults.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Tag on every ledger event this domain appends, and part of the resolution key.
    #: Events are append-only, so never rename it.
    name: str
    claim: type[Claim]
    decision: type[Decision]
    invalidation: type[SourceInvalidation]
    #: ``ln.Record`` type names a claim may be about.
    subject_types: tuple[str, ...]
    #: Predicates a human reviews. Only these move a subject through the queue.
    reviewed_predicates: tuple[str, ...]
    #: Predicates a source may hold several values of at once. The value joins the
    #: supersession key, so each value is its own position.
    multi_valued_predicates: tuple[str, ...] = ()
    #: ``ln.Record`` type a claim row is stored as.
    claim_type: str
    #: ``ln.Record`` type verdict rows were stored as.
    verdict_type: str
    invalidation_type: str
    #: ``ln.Feature`` holding a subject's review state.
    state_feature: str
    policy: Policy = no_policy
    #: What an agent may not accept: raise to refuse. A human may still accept it.
    #: Runs before any write.
    agent_may_accept: Policy = no_policy


REQUIRED = ("subject", "predicate", "value", "source", "source_id", "evidence")


def validate(claim: Claim, *, subject_row: dict[str, str] | None = None,
             policy: Policy = no_policy) -> None:
    """Raise unless the claim is citable and ``policy`` permits it.

    A whitespace-only field is refused, not stripped.
    """
    missing = [f for f in REQUIRED if not str(getattr(claim, f) or "").strip()]
    if missing:
        raise ValueError(f"{claim.subject}: claim is not citable, missing {missing}")

    policy(str(claim.predicate), claim.value,
           subject_row or {"canonical_name": claim.subject})


def check_claim_domain(claim: Claim, domain: ClaimDomain) -> None:
    """Raise unless ``claim`` belongs to ``domain``."""
    if not isinstance(claim, domain.claim):
        raise ValueError(
            f"{claim.subject}: this store holds {domain.claim.__name__} rows and was handed a "
            f"{type(claim).__name__}. A claim and the store's domain must be the same domain's"
        )


def check_invalidation_domain(invalidation: SourceInvalidation,
                              domain: ClaimDomain) -> None:
    """Raise unless ``invalidation`` belongs to ``domain``.

    Domains share source names such as ``human``; a cross-domain invalidation would void
    unrelated claims in bulk.
    """
    if not isinstance(invalidation, domain.invalidation):
        raise ValueError(
            f"{invalidation.source_id}: this store voids "
            f"{domain.invalidation.__name__} sources and was handed a "
            f"{type(invalidation).__name__}. An invalidation and the store's domain must be "
            "the same domain's"
        )


def check_subject_type(subject: str, type_name: str | None, *,
                       permitted: tuple[str, ...], where: str = "") -> None:
    """Raise unless ``subject`` resolved to one of the ``permitted`` types.

    ``type_name`` is None when nothing resolved. ``where`` names the branch searched.
    """
    allowed = " or ".join(permitted)
    located = f" on branch {where!r}" if where else ""
    if type_name is None:
        raise ValueError(
            f"no {allowed} record named {subject!r}{located}. A claim subject is a Record "
            "link, so the record has to exist on that branch before any claim about it can "
            "be written. Creating one is the domain's own missing-subject path"
        )
    if type_name not in permitted:
        raise ValueError(
            f"{subject!r}{located} is a {type_name}, and a claim subject in this domain must "
            f"be a {allowed}. A record type outside that set takes no claims"
        )


class Assertion(NamedTuple):
    """The slot a claim occupies. A later claim in the same slot supersedes the earlier one.

    The source is part of the key, so different sources never supersede each other.
    ``value`` is blank except on a multi-valued predicate.
    """

    subject: str
    predicate: str
    source: str
    source_id: str
    value: str = ""


def supersession_key(claim: Claim, multi_valued: Collection[str] = ()) -> Assertion:
    """The `Assertion` ``claim`` occupies, keyed on value for a ``multi_valued`` predicate."""
    predicate = str(claim.predicate)
    return Assertion(claim.subject, predicate, str(claim.source), claim.source_id,
                     claim.value if predicate in multi_valued else "")


def standing(rows: Sequence[tuple[Any, Assertion, str]],
             invalidations: Sequence[SourceInvalidation] = ()) -> list[Any]:
    """Handles of the assertions still standing, oldest first.

    ``rows`` is ``(handle, key, source_version)`` in creation order. Invalidated rows drop first,
    then the newest row per key wins.

    >>> a = Assertion("S-001", "disposition", "sop", "SOP-14")
    >>> b = Assertion("S-001", "disposition", "human", "rafael")
    >>> standing([(0, a, ""), (1, b, ""), (2, a, "")])
    [1, 2]
    """
    kept: dict[Assertion, tuple[int, Any]] = {}
    for position, (handle, key, source_version) in enumerate(rows):
        if any(void.voids(key, source_version) for void in invalidations):
            continue
        kept[key] = (position, handle)
    return [handle for _, handle in sorted(kept.values())]


def active(claims: Sequence[Claim],
           invalidations: Sequence[SourceInvalidation] = (), *,
           multi_valued: Collection[str] = ()) -> list[Claim]:
    """`standing` over claims: what the sources currently say, oldest first.

    ``claims`` must be in creation order. Nothing is deleted.
    """
    rows = [(index, supersession_key(claim, multi_valued), claim.source_version)
            for index, claim in enumerate(claims)]
    return [claims[index] for index in standing(rows, invalidations)]


def stored_duplicate(incoming: Claim, stored: list[Claim], *,
                     multi_valued: Collection[str] = ()) -> Claim | None:
    """The standing claim ``incoming`` repeats, or None when it says something new.

    ``stored`` is the standing claims for this subject and predicate, oldest first. Only the
    newest standing row from the same source is compared. Decisions and ``confidence`` are ignored.
    A superseded or voided row is never a duplicate.
    """
    key = supersession_key(incoming, multi_valued)
    for claim in reversed(stored):
        if supersession_key(claim, multi_valued) == key:
            same = (claim.model_dump(exclude={"confidence"})
                    == incoming.model_dump(exclude={"confidence"}))
            return claim if same else None
    return None


def in_effect(decisions: Sequence[Decision]) -> dict[str, Decision]:
    """The newest decision per claim id. ``decisions`` must be oldest first."""
    newest: dict[str, Decision] = {}
    for decision in decisions:
        newest[decision.claim_id] = decision
    return newest


def answers(claims: Sequence[Claim], decisions: Sequence[Decision],
            invalidations: Sequence[SourceInvalidation] = ()) -> list[Decision]:
    """The current answer: accepting decisions in effect on unvoided claims, oldest first.

    ``claims`` is the whole log, history included. Several answers may stand for one predicate.
    """
    voided = {short_id(c.claim_id()) for c in claims
              if any(v.voids(supersession_key(c), c.source_version) for v in invalidations)}
    known = {short_id(c.claim_id()) for c in claims}
    effect = in_effect(decisions)
    return [d for d in decisions
            if effect[d.claim_id] is d and d.outcome == Outcome.ACCEPTED
            and d.claim_id in known and d.claim_id not in voided]
