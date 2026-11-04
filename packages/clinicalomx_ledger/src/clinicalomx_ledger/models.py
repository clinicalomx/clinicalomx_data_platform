"""Ledger tables: plain Django ORM, append-only at the database.

    claim     what a source says about (domain, subject, predicate), with its confidence
    decision  what a human, or an agent under an approved policy, ruled on one claim
    release   which decisions were in effect when a dataset was published

Triggers (migrations 0002 and 0005) refuse UPDATE, DELETE and TRUNCATE for every client,
superuser included. See docs/runbook.md for break-glass.
CHECK constraints require model and version when actor_kind='agent'. An agent decision must
cite an approved `Policy`. ``run_uid`` is an opaque lamindb run uid, not a foreign key.
``schema_version`` is 1 on rows migrated from the six-table model and 2 otherwise.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

#: Schema version written on new rows.
SCHEMA_VERSION = 2


class ActorKind(models.TextChoices):
    HUMAN = "human"
    AGENT = "agent"


class Outcome(models.TextChoices):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def _typed(table: str, actor: str) -> list[models.CheckConstraint]:
    """CHECK constraints: every writer names itself; an agent also names its model and version."""
    return [
        models.CheckConstraint(name=f"{table}_names_its_actor", condition=~Q(**{actor: ""})),
        models.CheckConstraint(
            name=f"{table}_agent_identity_typed",
            condition=(
                (Q(actor_kind="agent") & ~Q(agent_model="") & ~Q(agent_model_version=""))
                | Q(actor_kind="human", agent_model="", agent_model_version="")
            ),
        ),
    ]


class _Actor(models.Model):
    actor_kind = models.CharField(max_length=8, choices=ActorKind.choices)
    agent_model = models.CharField(max_length=255, default="", blank=True)
    agent_model_version = models.CharField(max_length=64, default="", blank=True)
    created_at = models.DateTimeField(editable=False, db_default=Now(), db_index=True)

    class Meta:
        abstract = True


class LedgerEvent(_Actor):
    """The six-table model's shared columns, kept for `SourceInvalidationEvent`."""

    uid = models.CharField(max_length=64, unique=True, db_index=True)
    domain = models.CharField(max_length=64, db_index=True)
    schema_version = models.IntegerField(default=1)
    actor = models.CharField(max_length=255)

    class Meta:
        abstract = True
        constraints = _typed("%(class)s", "actor")


class SourceInvalidationEvent(LedgerEvent):
    source = models.CharField(max_length=255, db_index=True)
    source_id = models.CharField(max_length=255, db_index=True)
    source_version = models.CharField(max_length=255, default="", blank=True)
    reason = models.CharField(max_length=255)
    note = models.TextField(default="", blank=True)
    occurred_at = models.DateTimeField()

    class Meta(LedgerEvent.Meta):
        constraints = [
            *LedgerEvent.Meta.constraints,
            models.CheckConstraint(
                name="invalidation_states_its_reason",
                condition=~Q(reason=""),
            ),
        ]


class Claim(_Actor):
    """One assertion a source makes about a question, with what it takes to cite it.

    ``confidence`` is in [0, 1] or None. ``policy_version`` is blank when there was no policy.
    """

    uid = models.CharField(max_length=64, unique=True, db_index=True)
    schema_version = models.IntegerField(default=SCHEMA_VERSION)
    domain = models.CharField(max_length=64, db_index=True)
    subject = models.CharField(max_length=255, db_index=True)
    predicate = models.CharField(max_length=255, db_index=True)
    value = models.TextField()
    source = models.CharField(max_length=255, db_index=True)
    source_id = models.CharField(max_length=255, db_index=True)
    source_version = models.CharField(max_length=255, default="", blank=True)
    evidence = models.TextField()
    proposed_by = models.CharField(max_length=255)
    confidence = models.FloatField(null=True, default=None, blank=True)
    policy_version = models.CharField(max_length=64, default="", blank=True)
    run_uid = models.CharField(max_length=64, null=True, default=None, blank=True)

    class Meta:
        indexes = [models.Index(fields=["domain", "subject", "predicate"],
                                name="claim_question")]
        constraints = [
            *_typed("claim", "proposed_by"),
            models.CheckConstraint(
                name="claim_is_citable",
                condition=(
                    ~Q(subject="")
                    & ~Q(predicate="")
                    & ~Q(value="")
                    & ~Q(source="")
                    & ~Q(source_id="")
                    & ~Q(evidence="")
                ),
            ),
            models.CheckConstraint(
                name="claim_confidence_is_a_probability",
                condition=Q(confidence__isnull=True)
                | Q(confidence__gte=0.0, confidence__lte=1.0),
            ),
        ]


class Policy(models.Model):
    """A policy a human approved, under which an agent may decide. Only the curator role inserts."""

    version = models.CharField(max_length=64, unique=True)
    schema_version = models.IntegerField(default=SCHEMA_VERSION)
    approved_by = models.CharField(max_length=255)
    note = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(editable=False, db_default=Now(), db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(name="policy_names_its_approver",
                                   condition=~Q(approved_by="")),
            models.CheckConstraint(name="policy_has_a_version", condition=~Q(version="")),
        ]


class Decision(_Actor):
    """One ruling on one claim. The newest decision on a claim is in effect.

    An agent decides only under an approved ``policy``. ``decided_at`` is the decider's stated
    time; ``created_at`` is the append time.
    """

    uid = models.CharField(max_length=64, unique=True, db_index=True)
    schema_version = models.IntegerField(default=SCHEMA_VERSION)
    claim = models.ForeignKey(Claim, on_delete=models.PROTECT, related_name="decisions")
    outcome = models.CharField(max_length=8, choices=Outcome.choices, db_index=True)
    #: How a rejection generalises: rule | hint | one_off, blank otherwise.
    kind = models.CharField(max_length=16, default="", blank=True)
    reason = models.TextField(default="", blank=True)
    by = models.CharField(max_length=255)
    policy = models.ForeignKey(
        Policy, to_field="version", db_column="policy_version", on_delete=models.PROTECT,
        null=True, default=None, blank=True, related_name="decisions",
    )
    decided_at = models.DateTimeField()
    run_uid = models.CharField(max_length=64, null=True, default=None, blank=True)

    class Meta:
        constraints = [
            *_typed("decision", "by"),
            models.CheckConstraint(
                name="decision_outcome_is_known",
                condition=Q(outcome__in=["accepted", "rejected"]),
            ),
            models.CheckConstraint(
                name="agent_decides_under_an_approved_policy",
                condition=Q(actor_kind="human") | Q(policy__isnull=False),
            ),
        ]


class Release(_Actor):
    """A published set of decisions. `ReleaseEntry` is the set, ``manifest_hash`` its digest.

    ``run_uid`` is None when no run was open.
    """

    uid = models.CharField(max_length=64, unique=True, db_index=True)
    schema_version = models.IntegerField(default=SCHEMA_VERSION)
    domain = models.CharField(max_length=64, db_index=True)
    manifest_hash = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=64, default="", blank=True)
    by = models.CharField(max_length=255)
    run_uid = models.CharField(max_length=64, null=True, default=None, blank=True)
    decisions = models.ManyToManyField(Decision, through="ReleaseEntry", related_name="+")

    class Meta:
        constraints = [*_typed("release", "by")]


class ReleaseEntry(models.Model):
    """One decision a release pins."""

    release = models.ForeignKey(Release, on_delete=models.PROTECT, related_name="entries")
    decision = models.ForeignKey(Decision, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["release", "decision"], name="one_entry_per_pair"),
        ]
