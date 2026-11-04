"""Claim stores: one `ClaimStore` protocol, a memory backend and a ledger backend.

Each answers, oldest first: `held` (what sources currently say about a subject), `decisions`
(every ruling), `answers` (claims whose newest decision accepts them) and `invalidations`.
`held` is a projection: newest claim per `model.Assertion`, minus voided sources.
``held(..., history=True)`` returns the whole log. Nothing is deleted.

`LedgerClaimStore` writes append-only ledger rows through `clinicalomx_ledger.adapter`.
Subjects and their review state live on lamindb Records, which needs the ``store`` extra.
The producing run is the ambient ``ln.context.run``. Only a human calls `invalidate`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .gate import Judgement, ReviewState, decisions_for, subject_state_for
from .lineage import Lineage
from .lineage import of as lineage_of
from .model import (
    Claim,
    ClaimDomain,
    Decision,
    Outcome,
    SourceInvalidation,
    active,
    answers,
    check_claim_domain,
    check_invalidation_domain,
    check_subject_type,
    short_id,
    stored_duplicate,
    validate,
)


def _moment(at: str, *, what: str) -> datetime:
    """Parse a human ISO date into an aware datetime; a bare date is midnight UTC."""
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError as refused:
        raise ValueError(
            f"{what} {at!r} is not an ISO date, and the ledger stores a decision "
            "time as a timestamp"
        ) from refused
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _at_string(moment: datetime | None) -> str:
    """Inverse of `_moment`."""
    if moment is None:
        return ""
    moment = moment.astimezone(UTC)
    if (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.date().isoformat()
    return moment.isoformat()


def _written(claim: Claim, log: Sequence[Claim]) -> None:
    """Refuse a ruling on a claim this store never wrote."""
    wanted = short_id(claim.claim_id())
    if not any(short_id(c.claim_id()) == wanted for c in log):
        raise ValueError(f"{claim.subject}: no claim {wanted!r} has been written, so there is "
                         "nothing to rule on")


@runtime_checkable
class ClaimStore(Protocol):
    """What the curator needs of a claim backend. Runtime checks cover method names only.

    Example:
        A machine proposes a disposition: one claim, no decision, subject unresolved.

    >>> from clinicalomx_ledger.grammar.example import (
    ...     EXAMPLE_DOMAIN, SampleClaim, SamplePredicate)
    >>> from clinicalomx_ledger.grammar.gate import Judgement
    >>> store = MemoryClaimStore(EXAMPLE_DOMAIN, subject_types={"S-001": "Sample"})
    >>> def disposition(**over):
    ...     base = dict(subject="S-001", predicate=SamplePredicate.DISPOSITION,
    ...                 value="retain", source="sop", source_id="SOP-14",
    ...                 source_version="rev-3", evidence="Consent on file.", confidence=0.8)
    ...     return SampleClaim(**{**base, **over})
    >>> _ = store.add(disposition())
    >>> store.in_state("unresolved"), store.decisions("S-001")
    (['S-001'], [])

    A human accepts it, and the claim becomes the answer:

    >>> call = Judgement(by="rafael", at="2026-08-24", reason="block intact")
    >>> _ = store.add(disposition(), outcome="accepted", judgement=call)
    >>> store.in_state("accepted"), [d.value for d in store.answers("S-001")]
    (['S-001'], ['retain'])

    One claim row, carrying its confidence:

    >>> [(c.value, c.confidence) for c in store.held("S-001")]
    [('retain', 0.8)]

    A second curator's sign-off adds a decision, not a claim. The same decision twice is a no-op:

    >>> for _ in range(2):
    ...     _ = store.add(disposition(), outcome="accepted",
    ...                   judgement=Judgement(by="wenchao", at="2026-08-25"))
    >>> len(store.held("S-001")), len(store.decisions("S-001"))
    (1, 2)

    A rejection is a later decision; `decide` rules without re-asserting:

    >>> _ = store.decide(disposition(), outcome="rejected",
    ...                  judgement=Judgement(by="rafael", at="2026-08-26", kind="one_off"))
    >>> store.answers("S-001"), store.in_state("rejected")
    ([], ['S-001'])

    The same source changing its value supersedes; the log keeps both:

    >>> _ = store.add(disposition(value="discard", evidence="Consent withdrawn."))
    >>> [c.value for c in store.held("S-001")]
    ['discard']
    >>> [c.value for c in store.held("S-001", history=True)]
    ['retain', 'discard']

    A different source disagreeing does not supersede:

    >>> _ = store.add(disposition(source="human", source_id="rafael", source_version=""))
    >>> sorted((str(c.source), c.value) for c in store.held("S-001"))
    [('human', 'retain'), ('sop', 'discard')]

    Voiding the SOP revision drops its claims and leaves the human's:

    >>> from clinicalomx_ledger.grammar.example import SampleSourceInvalidation
    >>> _ = store.invalidate(SampleSourceInvalidation(
    ...     source="sop", source_id="SOP-14", source_version="rev-3",
    ...     reason="rev-3 shifted the disposition column",
    ...     by="rafael", at="2026-08-27"))
    >>> [(str(c.source), c.value) for c in store.held("S-001")]
    [('human', 'retain')]
    >>> len(store.held("S-001", history=True))
    3

    The memory backend has no runs:

    >>> store.lineage(disposition()) is None
    True
    """

    def add(self, claim: Claim, *, outcome: Outcome | None = None,
            judgement: Judgement | None = None,
            subject_row: dict[str, str] | None = None) -> Claim: ...

    def decide(self, claim: Claim, *, outcome: Outcome,
               judgement: Judgement) -> list[Decision]: ...

    def invalidate(self, invalidation: SourceInvalidation) -> SourceInvalidation: ...

    def lineage(self, claim: Claim) -> Lineage | None: ...

    def held(self, subject: str, *, predicate: str | None = None,
             history: bool = False) -> list[Claim]: ...

    def decisions(self, subject: str, *, predicate: str | None = None) -> list[Decision]: ...

    def answers(self, subject: str, *, predicate: str | None = None) -> list[Decision]: ...

    def invalidations(self) -> list[SourceInvalidation]: ...

    def set_state(self, subject: str, state: str) -> None: ...

    def in_state(self, state: str) -> list[str]: ...


@dataclass
class MemoryClaimStore:
    """Claims in a list, for tests and notebooks."""

    domain: ClaimDomain
    claims: list[Claim] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    #: Subject name -> record type. Absent names take the domain's first subject type.
    subject_types: dict[str, str] = field(default_factory=dict)
    #: Every ruling, append-only, in creation order.
    rulings: list[Decision] = field(default_factory=list)
    #: Voided sources, append-only. Reaches every subject the source spoke about.
    voids: list[SourceInvalidation] = field(default_factory=list)

    def add(self, claim: Claim, *, outcome: Outcome | None = None,
            judgement: Judgement | None = None,
            subject_row: dict[str, str] | None = None) -> Claim:
        """Write the claim and its decision, skipping whichever repeats what already stands."""
        check_claim_domain(claim, self.domain)
        validate(claim, subject_row=subject_row, policy=self.domain.policy)
        self._subject(claim.subject)
        already = stored_duplicate(claim, self.held(claim.subject, predicate=claim.predicate),
                                   multi_valued=self.domain.multi_valued_predicates)
        made = decisions_for(
            self.domain, claim, outcome=outcome,
            decisions=self.decisions(claim.subject, predicate=claim.predicate),
            judgement=judgement,
        )
        if already is None:
            self.claims.append(claim)
        self.rulings.extend(made)
        self._follow_state(claim.subject)
        return already if already is not None else claim

    def decide(self, claim: Claim, *, outcome: Outcome,
               judgement: Judgement) -> list[Decision]:
        """Rule on an already-written claim without re-asserting it."""
        _written(claim, self.held(claim.subject, history=True))
        made = decisions_for(
            self.domain, claim, outcome=outcome,
            decisions=self.decisions(claim.subject, predicate=claim.predicate),
            judgement=judgement,
        )
        self.rulings.extend(made)
        self._follow_state(claim.subject)
        return made

    def _follow_state(self, subject: str) -> None:
        """Move the subject to the state its claims and decisions imply."""
        state = subject_state_for(self.domain, self.held(subject), self.decisions(subject))
        if state is not None:
            self.set_state(subject, state)

    def set_state(self, subject: str, state: str) -> None:
        """Move the subject to ``state``. Raises on a value that is not a `ReviewState`."""
        self._subject(subject)
        self.states[subject] = ReviewState(state)

    def _subject(self, subject: str) -> str:
        """The subject name, after checking its type is permitted."""
        check_subject_type(
            subject,
            self.subject_types.get(subject, self.domain.subject_types[0]),
            permitted=self.domain.subject_types,
        )
        return subject

    def in_state(self, state: str) -> list[str]:
        return sorted(s for s, at in self.states.items() if at == state)

    def held(self, subject: str, *, predicate: str | None = None,
             history: bool = False) -> list[Claim]:
        """What the sources currently say about ``subject``, oldest first.

        ``history=True`` returns the whole log, superseded and voided rows included.
        """
        log = [c for c in self.claims
               if c.subject == subject
               and (predicate is None or c.predicate == predicate)]
        return log if history else active(
            log, self.voids, multi_valued=self.domain.multi_valued_predicates)

    def decisions(self, subject: str, *, predicate: str | None = None) -> list[Decision]:
        """Rulings on ``subject``'s claims, oldest first."""
        return [d for d in self.rulings
                if d.subject == subject
                and (predicate is None or d.predicate == predicate)]

    def answers(self, subject: str, *, predicate: str | None = None) -> list[Decision]:
        """Accepting decisions in effect for ``subject``, oldest first."""
        return answers(self.held(subject, predicate=predicate, history=True),
                       self.decisions(subject, predicate=predicate), self.voids)

    def invalidations(self) -> list[SourceInvalidation]:
        """Every void, oldest first."""
        return list(self.voids)

    def invalidate(self, invalidation: SourceInvalidation) -> SourceInvalidation:
        """Append one void. Never deduplicated."""
        check_invalidation_domain(invalidation, self.domain)
        self.voids.append(invalidation)
        return invalidation

    def lineage(self, claim: Claim) -> Lineage | None:
        """Always None: this backend has no runs."""
        return None


@dataclass
class LedgerClaimStore:
    """Claims and decisions in the governance ledger, under the domain's tag.

    Imports of lamindb and the ledger are deferred: importing lamindb connects to the instance.
    ``branch``, ``domain``, ``actor`` and ``actor_kind`` have no default. ``branch`` scopes
    subjects and review state only; ledger rows are instance-global. An agent session needs
    ``actor_kind="agent"``, its model and version, and a ``policy_version``. A decision is
    written as whoever decided it: a judgement by another name is typed human.
    """

    branch: str
    domain: ClaimDomain
    actor: str
    actor_kind: str
    agent_model: str = ""
    agent_model_version: str = ""
    policy_version: str = ""
    _resolved_branch: Any = field(default=None, init=False, repr=False, compare=False)

    def add(self, claim: Claim, *, outcome: Outcome | None = None,
            judgement: Judgement | None = None,
            subject_row: dict[str, str] | None = None) -> Claim:
        """`MemoryClaimStore.add` against the ledger. Every refusal happens before any write."""
        check_claim_domain(claim, self.domain)
        validate(claim, subject_row=subject_row, policy=self.domain.policy)
        subject = self._subject(claim.subject)
        already = stored_duplicate(claim, self.held(claim.subject, predicate=claim.predicate),
                                   multi_valued=self.domain.multi_valued_predicates)
        made = decisions_for(
            self.domain, claim, outcome=outcome,
            decisions=self.decisions(claim.subject, predicate=claim.predicate),
            judgement=judgement,
        )
        self._agent_may_accept(claim, made, subject_row)
        if already is None:
            self._write(claim)
        self._decide(claim.subject, made)
        self._follow_subject_state(subject)
        return already if already is not None else claim

    def decide(self, claim: Claim, *, outcome: Outcome,
               judgement: Judgement) -> list[Decision]:
        """`MemoryClaimStore.decide` against the ledger."""
        subject = self._subject(claim.subject)
        _written(claim, self.held(claim.subject, history=True))
        made = decisions_for(
            self.domain, claim, outcome=outcome,
            decisions=self.decisions(claim.subject, predicate=claim.predicate),
            judgement=judgement,
        )
        self._agent_may_accept(claim, made)
        self._decide(claim.subject, made)
        self._follow_subject_state(subject)
        return made

    def _agent_may_accept(self, claim: Claim, made: Sequence[Decision],
                          subject_row: dict[str, str] | None = None) -> None:
        """Refuse an agent's own acceptance that `agent_may_accept` forbids."""
        if self.actor_kind != "agent":
            return
        if any(d.by == self.actor and d.outcome == Outcome.ACCEPTED for d in made):
            self.domain.agent_may_accept(str(claim.predicate), claim.value,
                                         subject_row or {"canonical_name": claim.subject})

    def _write(self, claim: Claim) -> None:
        """Write the claim row as this session's actor."""
        from .. import adapter

        adapter.append_claim(
            domain=self.domain.name,
            subject=claim.subject,
            predicate=str(claim.predicate),
            value=claim.value,
            source=str(claim.source),
            source_id=claim.source_id,
            source_version=claim.source_version,
            evidence=claim.evidence,
            proposed_by=self.actor,
            actor_kind=self.actor_kind,
            agent_model=self.agent_model,
            agent_model_version=self.agent_model_version,
            confidence=claim.confidence,
            policy_version=self.policy_version,
            run_uid=self._run_uid(),
            multi_valued=self.domain.multi_valued_predicates,
        )

    def _decide(self, subject: str, made: Sequence[Decision]) -> None:
        """Append the decision rows `add` built, each as whoever decided it.

        A decider other than this session's actor is typed human. An agent session's own
        decision is typed agent under the session's policy; the database checks that policy.
        Each decision attaches to the newest claim row with its claim id.
        """
        from .. import adapter

        if not made:
            return
        uid_of = {short_id(self._read(event).claim_id()): event.uid
                  for event in self._claim_events(subject)}
        for decided in made:
            agent = self.actor_kind == "agent" and decided.by == self.actor
            adapter.append_decision(
                domain=self.domain.name,
                claim_uid=uid_of[decided.claim_id],
                outcome=str(decided.outcome),
                by=decided.by,
                actor_kind="agent" if agent else "human",
                agent_model=self.agent_model if agent else "",
                agent_model_version=self.agent_model_version if agent else "",
                policy_version=(self.policy_version or None) if agent else None,
                decided_at=_moment(decided.at, what="decision at"),
                kind=str(decided.kind),
                reason=decided.reason,
                run_uid=self._run_uid(),
            )

    def set_state(self, subject: str, state: str) -> None:
        from .gate import set_subject_state

        set_subject_state(self._subject(subject), state,
                          feature=self.domain.state_feature)

    def in_state(self, state: str) -> list[str]:
        """Subjects in ``state``, via an indexed lamindb filter."""
        from .gate import subjects_in_state

        found = subjects_in_state(state, branch=self._branch(),
                                  types=self.domain.subject_types,
                                  feature=self.domain.state_feature)
        return sorted(r.name for r in found)

    def held(self, subject: str, *, predicate: str | None = None,
             history: bool = False) -> list[Claim]:
        """`MemoryClaimStore.held` off the ledger log."""
        log = [self._read(event) for event in self._claim_events(subject, predicate)]
        return log if history else active(
            log, self.invalidations(), multi_valued=self.domain.multi_valued_predicates)

    def decisions(self, subject: str, *, predicate: str | None = None) -> list[Decision]:
        """`MemoryClaimStore.decisions` off the ledger log, claim uids mapped to claim ids."""
        from .. import adapter

        rows = adapter.decision_log(self.domain.name, subject,
                                    None if predicate is None else str(predicate))
        if not rows:
            return []
        id_of = {event.uid: short_id(self._read(event).claim_id())
                 for event in self._claim_events(subject)}
        return [self.domain.decision(
            subject=row.subject,
            predicate=row.predicate,
            value=row.value,
            claim_id=id_of[row.claim_uid],
            outcome=row.outcome,
            kind=row.kind,
            reason=row.reason,
            by=row.by,
            at=_at_string(row.decided_at),
        ) for row in rows]

    def answers(self, subject: str, *, predicate: str | None = None) -> list[Decision]:
        """`MemoryClaimStore.answers` off the ledger."""
        return answers(self.held(subject, predicate=predicate, history=True),
                       self.decisions(subject, predicate=predicate), self.invalidations())

    def invalidations(self) -> list[SourceInvalidation]:
        """This domain's voids, oldest first."""
        from .. import adapter

        return [self.domain.invalidation(
            source=event.source,
            source_id=event.source_id,
            source_version=event.source_version,
            reason=event.reason,
            by=event.actor,
            at=_at_string(event.occurred_at),
        ) for event in adapter.invalidation_log(self.domain.name)]

    def invalidate(self, invalidation: SourceInvalidation) -> SourceInvalidation:
        """Append one void as the human who signed it. Never deduplicated, never cites a run."""
        from .. import adapter

        check_invalidation_domain(invalidation, self.domain)
        adapter.append_invalidation(
            domain=self.domain.name,
            source=str(invalidation.source),
            source_id=invalidation.source_id,
            source_version=invalidation.source_version,
            reason=invalidation.reason,
            occurred_at=_moment(invalidation.at, what="invalidation at"),
            actor=invalidation.by,
            actor_kind="human",
        )
        return invalidation

    def lineage(self, claim: Claim) -> Lineage | None:
        """Lineage of the run that produced ``claim``, or None when there is no run.

        The last matching row wins. The run uid is returned even when the run row is gone.
        """
        import lamindb as ln

        matched = [event for event in self._claim_events(claim.subject)
                   if self._read(event) == claim]
        if not matched or not matched[-1].run_uid:
            return None
        run_uid = matched[-1].run_uid
        run = ln.Run.filter(uid=run_uid).one_or_none()
        return lineage_of(run) if run is not None else Lineage(run_uid=run_uid)

    def claim(self, uid: str) -> Claim | None:
        """The claim at ledger row ``uid`` in this domain, or None."""
        from .. import adapter

        event = adapter.claim(uid)
        if event is None or event.domain != self.domain.name:
            return None
        return self._read(event)

    def _claim_events(self, subject: str, predicate: str | None = None):
        from .. import adapter

        return adapter.claim_log(self.domain.name, subject,
                                 None if predicate is None else str(predicate))

    def _read(self, event) -> Claim:
        """Rebuild a domain claim from a ledger row."""
        return self.domain.claim(
            subject=event.subject,
            predicate=event.predicate,
            value=event.value,
            source=event.source,
            source_id=event.source_id,
            source_version=event.source_version,
            evidence=event.evidence,
            confidence=event.confidence,
        )

    def _run_uid(self) -> str | None:
        """The ambient run's uid, or None when no run is open."""
        import lamindb as ln

        uid = getattr(ln.context.run, "uid", None)
        return str(uid) if uid else None

    def _follow_subject_state(self, subject_record) -> None:
        """Set the subject's review state from its claims and decisions.

        This is a projection; `set_state` can make it disagree with the ledger.
        """
        from .gate import set_subject_state

        name = subject_record.name
        state = subject_state_for(self.domain, self.held(name), self.decisions(name))
        if state is not None:
            set_subject_state(subject_record, state, feature=self.domain.state_feature)

    def _branch(self):
        """The branch, resolved once."""
        if self._resolved_branch is None:
            import lamindb as ln

            found = ln.Branch.filter(name=self.branch).one_or_none()
            if found is None:
                raise ValueError(f"no branch named {self.branch!r} in this instance")
            self._resolved_branch = found
        return self._resolved_branch

    def _subject(self, subject: str):
        """The record a claim is about on this branch, filtered by permitted types."""
        found = self._scoped(
            name=subject, type__name__in=self.domain.subject_types).one_or_none()
        check_subject_type(subject, self._type_of(subject, found),
                           permitted=self.domain.subject_types, where=self.branch)
        return found

    def _scoped(self, queryset=None, **expressions):
        """One filter call that always names the branch. Every record query goes through here.

        lamindb scopes each call with no branch to main plus the ambient branch.
        """
        import lamindb as ln

        base = ln.Record if queryset is None else queryset
        return base.filter(branch=self._branch(), **expressions)

    def _type_of(self, subject: str, found) -> str | None:
        """The record type of ``subject``, queried only when the permitted lookup missed."""
        if found is not None:
            return _type_name(found)
        other = self._scoped(name=subject).first()
        return None if other is None else _type_name(other)


def _type_name(record) -> str | None:
    """The type a record is an instance of, or None for a type record."""
    return getattr(getattr(record, "type", None), "name", None)
