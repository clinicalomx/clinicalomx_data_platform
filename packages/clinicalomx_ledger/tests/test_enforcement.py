"""Every ledger table refuses mutation at the database, from the ORM and from raw superuser SQL.

Break-glass is owner-only DDL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import close_old_connections
from django.db.models.deletion import ProtectedError
from django.db.utils import InternalError
from psycopg2 import errors as pgerrors

from clinicalomx_ledger import services
from clinicalomx_ledger.models import (
    Claim,
    Decision,
    Policy,
    Release,
    ReleaseEntry,
    SourceInvalidationEvent,
)
from conftest import AGENT, HUMAN, as_role, superuser, table

TABLES = (Claim, Decision, Policy, Release, ReleaseEntry, SourceInvalidationEvent)


@pytest.fixture(scope="module", autouse=True)
def seeded():
    claim = services.create_claim(
        domain="marker", subject="ENF-CD4", predicate="segmentation_role",
        value="marker", source="uniprot", source_id="ENF-P01730",
        evidence="synthetic: enforcement fixture", proposed_by="synthetic-agent", **AGENT,
    )
    services.approve_policy(version="ENF-policy-1", approved_by="synthetic-human")
    services.decide(claim=claim, outcome="accepted", by="synthetic-human",
                    decided_at=datetime.now(UTC), reason="synthetic", **HUMAN)
    services.cut_release(domain="marker", by="synthetic-human", **HUMAN)
    services.invalidate_source(domain="marker", source="url", source_id="ENF-void",
                               reason="synthetic", actor="synthetic-human", **HUMAN)
    yield


def test_every_table_holds_a_row_to_attack():
    assert all(model.objects.exists() for model in TABLES)


def test_instance_save_refused_by_trigger():
    claim = Claim.objects.get(source_id="ENF-P01730")
    claim.value = "tampered"
    with pytest.raises(InternalError):
        claim.save()
    close_old_connections()


@pytest.mark.parametrize("model, change", [
    (Claim, {"value": "bulk"}),
    (Decision, {"outcome": "rejected"}),
    (Policy, {"note": "rewritten"}),
    (Release, {"manifest_hash": "0" * 64}),
    (SourceInvalidationEvent, {"reason": "rewritten"}),
], ids=lambda x: getattr(x, "__name__", ""))
def test_queryset_update_refused_by_trigger(model, change):
    with pytest.raises(InternalError):
        model.objects.all().update(**change)
    close_old_connections()


def test_a_ruled_claim_cannot_be_deleted_from_under_its_decision():
    with pytest.raises(ProtectedError):
        Claim.objects.get(source_id="ENF-P01730").delete()
    close_old_connections()


def test_instance_and_queryset_delete_refused_by_trigger():
    services.create_claim(
        domain="marker", subject="ENF-CD4", predicate="segmentation_role",
        value="doomed", source="url", source_id="ENF-sacrifice",
        evidence="synthetic sacrificial row", proposed_by="synthetic-agent", **AGENT,
    )
    sacrifice = Claim.objects.get(source_id="ENF-sacrifice")
    with pytest.raises(InternalError):
        sacrifice.delete()
    close_old_connections()
    with pytest.raises(InternalError):
        Claim.objects.filter(source_id="ENF-sacrifice").delete()
    close_old_connections()


@pytest.mark.parametrize("model", TABLES, ids=lambda m: m.__name__)
@pytest.mark.parametrize("verb, statement", [
    ("UPDATE", "UPDATE {t} SET id = id"),
    ("DELETE", "DELETE FROM {t}"),
    ("TRUNCATE", "TRUNCATE {t} CASCADE"),
])
def test_superuser_rewrites_refused_by_the_database_in_its_own_words(model, verb, statement):
    with superuser() as admin:
        with pytest.raises(pgerrors.RaiseException) as refused:
            with admin.cursor() as cur:
                cur.execute(statement.format(t=table(model)))
        admin.rollback()
    assert f"{table(model)} is append-only; {verb} refused" in str(refused.value)


def test_writer_role_update_refused_by_grants_before_trigger():
    agent = as_role("clinicalomx_ledger_agent")
    try:
        with pytest.raises(pgerrors.InsufficientPrivilege):
            with agent.cursor() as cur:
                cur.execute(f"UPDATE {table(Decision)} SET reason = 'x'")
    finally:
        agent.close()


def test_break_glass_is_owner_only():
    # DISABLE TRIGGER USER needs ownership, and writers never own the tables.
    agent = as_role("clinicalomx_ledger_agent")
    try:
        with pytest.raises(pgerrors.InsufficientPrivilege):
            with agent.cursor() as cur:
                cur.execute(f"ALTER TABLE {table(Claim)} DISABLE TRIGGER USER")
    finally:
        agent.close()
    # The owning identity can run break-glass DDL (docs/runbook.md).
    with superuser() as admin:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"ALTER TABLE {table(Claim)} DISABLE TRIGGER USER")
            cur.execute(f"UPDATE {table(Claim)} SET value = value")
            cur.execute(f"ALTER TABLE {table(Claim)} ENABLE TRIGGER USER")
        with admin.cursor() as cur:
            with pytest.raises(pgerrors.RaiseException):
                cur.execute(f"UPDATE {table(Claim)} SET value = value")
