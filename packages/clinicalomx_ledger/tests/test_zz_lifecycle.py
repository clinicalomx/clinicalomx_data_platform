"""Migrations match the models; exit runbook; rollback and redeploy; six-table row migration.

Named zz so the schema-mutating tests run after every data test.
"""

from __future__ import annotations

import importlib
import subprocess
from datetime import UTC, datetime
from urllib.parse import urlsplit

import psycopg2
import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
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
from conftest import (
    AGENT,
    CONTAINER_ID,
    DB_URL,
    HUMAN,
    ROLES,
    activate_role_logins,
    as_role,
    superuser,
    table,
)

EVENT_MODELS = (Claim, Decision, Policy, Release, ReleaseEntry, SourceInvalidationEvent)

_ddl = importlib.import_module("clinicalomx_ledger.migrations.0005_three_table_model")

SIX_TABLE_MODEL = ("clinicalomx_ledger", "0004_resolved_state_per_domain")


def in_container(*cmd: str) -> None:
    subprocess.run(["docker", "exec", CONTAINER_ID, *cmd], check=True)


def every_answer() -> set[tuple]:
    domains = set(Claim.objects.values_list("domain", flat=True))
    return {(d.claim.domain, d.claim.subject, d.claim.predicate, d.claim.value, d.uid)
            for domain in domains for d in services.answers(domain)}


def test_shipped_migrations_match_the_models():
    call_command("makemigrations", "clinicalomx_ledger", "--check", "--dry-run",
                 verbosity=0)


def test_exit_runbook_reapplies_the_ddl_after_a_table_scoped_dump():
    # Row triggers fire on rows, so each attacked table needs one.
    seed = services.create_claim(
        domain="marker", subject="EXIT-seed", predicate="segmentation_role",
        value="marker", source="url", source_id="EXIT-seed",
        evidence="synthetic: exit rehearsal seed", proposed_by="synthetic-human", **HUMAN,
    )
    services.decide(claim=seed, outcome="accepted", by="synthetic-human",
                    decided_at=datetime(2026, 9, 1, tzinfo=UTC), **HUMAN)
    tables = [table(model) for model in EVENT_MODELS]
    flags = [flag for name in tables for flag in ("-t", name)]
    in_container("pg_dump", "-U", "test", "-d", "test", *flags,
                 "-f", "/tmp/exit_dump.sql")
    in_container("psql", "-U", "test", "-d", "test", "-q",
                 "-c", "DROP DATABASE IF EXISTS exitdb")
    in_container("psql", "-U", "test", "-d", "test", "-q",
                 "-c", "CREATE DATABASE exitdb")
    # No ON_ERROR_STOP: the dump's CREATE TRIGGER lines fail and the data restores anyway.
    in_container("psql", "-U", "test", "-d", "exitdb", "-q",
                 "-f", "/tmp/exit_dump.sql")
    parts = urlsplit(DB_URL)
    exit_url = f"{parts.scheme}://{parts.netloc}/exitdb"
    conn = psycopg2.connect(exit_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table(Claim)}")
            assert cur.fetchone()[0] == Claim.objects.count()
            # A table-scoped dump drops enforcement.
            cur.execute(f"UPDATE {table(Claim)} SET value = value")
            cur.execute(_ddl.APPEND_ONLY_SQL)
            # psycopg2 leaves a multi-statement execute in an open transaction; close it.
            conn.commit()
            for model in (Claim, Decision):
                for statement in (
                    f"UPDATE {table(model)} SET id = id",
                    f"DELETE FROM {table(model)}",
                    f"TRUNCATE {table(model)} CASCADE",
                ):
                    with pytest.raises(pgerrors.RaiseException):
                        cur.execute(statement)
                    conn.rollback()
    finally:
        conn.close()
        # Drop the GRANTs the restore replayed into exitdb; DROP OWNED BY reaches only the
        # current database (docs/runbook.md).
        in_container("psql", "-U", "test", "-d", "test", "-q",
                     "-c", "DROP DATABASE exitdb WITH (FORCE)")


def test_rollback_redeploy_cycle_keeps_roles_grants_and_rows():
    counts_before = {table(m): m.objects.count() for m in EVENT_MODELS}
    answers_before = every_answer()
    flags = [flag for m in EVENT_MODELS for flag in ("-t", table(m))]
    in_container("pg_dump", "-U", "test", "-d", "test", "--data-only",
                 "--disable-triggers", *flags, "-f", "/tmp/gov_backup.sql")

    call_command("migrate", "clinicalomx_ledger", "zero",
                 verbosity=0, interactive=False)
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table(Claim),))
        assert cur.fetchone()[0] is None, "rollback left tables behind"
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                    (list(ROLES),))
        assert cur.fetchone()[0] == 0, "rollback left roles behind"

    call_command("migrate", "clinicalomx_ledger", verbosity=0, interactive=False)
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("SELECT rolname, rolcanlogin FROM pg_roles"
                    " WHERE rolname = ANY(%s)", (list(ROLES),))
        rows = dict(cur.fetchall())
        assert set(rows) == set(ROLES), "redeploy did not recreate the roles"
        assert not any(rows.values()), "a migration-created role can log in"

    in_container("psql", "-U", "test", "-d", "test", "-q",
                 "-v", "ON_ERROR_STOP=1", "-f", "/tmp/gov_backup.sql")
    restored = {table(m): m.objects.count() for m in EVENT_MODELS}
    assert restored == counts_before

    # A data-only restore leaves the identity sequences behind the rows; reset them.
    with superuser() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name in restored:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{name}', 'id'),"
                    f" coalesce((SELECT max(id) FROM {name}), 1))"
                )

    activate_role_logins()
    agent = as_role("clinicalomx_ledger_agent")
    try:
        with pytest.raises(pgerrors.InsufficientPrivilege):
            with agent.cursor() as cur:
                cur.execute(f"UPDATE {table(Claim)} SET value = 'x'")
    finally:
        agent.close()
    with superuser() as admin:
        with pytest.raises(pgerrors.RaiseException):
            with admin.cursor() as cur:
                cur.execute(f"UPDATE {table(Decision)} SET reason = 'x'")

    assert every_answer() == answers_before, "the answer is a read, so it came back whole"

    services.create_claim(
        domain="marker", subject="LIF-CD45", predicate="segmentation_role",
        value="marker", source="url", source_id="LIF-after-restore",
        evidence="synthetic: post-restore write probe", proposed_by="synthetic-human",
        **HUMAN,
    )


def _old_answer(old, domain: str, subject: str, predicate: str) -> list[str]:
    """The six-table model's answer: newest unretracted verdict's claim value when approved."""
    verdicts = [v for v in old.get_model("clinicalomx_ledger", "VerdictEvent").objects
                .filter(domain=domain, subject=subject, predicate=predicate)
                .order_by("created_at", "id") if not v.retractions.exists()]
    if not verdicts or verdicts[-1].to_status != "approved":
        return []
    return [verdicts[-1].value]


def test_six_table_rows_migrate_and_serve_the_answer_they_served():
    """Runs last: rolling back to the six-table model drops every three-table row."""
    call_command("migrate", *SIX_TABLE_MODEL, verbosity=0, interactive=False)
    old = MigrationExecutor(connection).loader.project_state(SIX_TABLE_MODEL).apps
    ClaimEvent = old.get_model("clinicalomx_ledger", "ClaimEvent")
    VerdictEvent = old.get_model("clinicalomx_ledger", "VerdictEvent")
    VerdictSupport = old.get_model("clinicalomx_ledger", "VerdictSupport")
    RetractionEvent = old.get_model("clinicalomx_ledger", "RetractionEvent")
    agent = {"actor": "synthetic-agent", **AGENT}
    human = {"actor": "synthetic-human", **HUMAN}

    def claim(uid, subject, value, source_id, **typing):
        return ClaimEvent.objects.create(
            uid=uid, domain="marker", subject=subject, predicate="segmentation_role",
            value=value, source="uniprot", source_id=source_id, evidence="synthetic: old row",
            proposed_in_run_uid="run-mig-1", **(typing or agent))

    def verdict(uid, subject, value, status, cited, day):
        decided = status != "proposed"
        row = VerdictEvent.objects.create(
            uid=uid, domain="marker", subject=subject, predicate="segmentation_role",
            value=value, to_status=status, reason=f"synthetic: {status}",
            decided_by="synthetic-human" if decided else "",
            decided_at=datetime(2026, 8, day, tzinfo=UTC) if decided else None, **human)
        for c in cited:
            VerdictSupport.objects.create(verdict=row, claim=c)
        return row

    accepted = claim("clm-mig-a", "MIG-A", "marker", "MIG-P-a")
    verdict("vrd-mig-a", "MIG-A", "marker", "approved", [accepted], 1)

    replaced = claim("clm-mig-b1", "MIG-B", "marker", "MIG-P-b")
    verdict("vrd-mig-b1", "MIG-B", "marker", "approved", [replaced], 1)
    human_read = claim("clm-mig-b2", "MIG-B", "cyto_structural", "rafael", **human)
    verdict("vrd-mig-b2", "MIG-B", "cyto_structural", "approved", [human_read], 2)

    rejected = claim("clm-mig-c", "MIG-C", "marker", "MIG-P-c")
    verdict("vrd-mig-c1", "MIG-C", "marker", "approved", [rejected], 1)
    verdict("vrd-mig-c2", "MIG-C", "marker", "rejected", [rejected], 2)

    proposed = claim("clm-mig-d", "MIG-D", "marker", "MIG-P-d")
    verdict("vrd-mig-d", "MIG-D", "marker", "proposed", [proposed], 1)

    retracted = claim("clm-mig-e", "MIG-E", "marker", "MIG-P-e")
    voided = verdict("vrd-mig-e", "MIG-E", "marker", "approved", [retracted], 1)
    RetractionEvent.objects.create(
        uid="ret-mig-e", domain="marker", verdict=voided, kind="retraction",
        reason="synthetic: approved in error", occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
        **human)

    uncited = claim("clm-mig-f", "MIG-F", "marker", "MIG-P-f")
    verdict("vrd-mig-f", "MIG-F", "nucleus", "approved", [uncited], 1)

    first = claim("clm-mig-g1", "MIG-G", "marker", "MIG-P-g")
    claim("clm-mig-g2", "MIG-G", "nucleus", "MIG-P-g")
    reasserted = claim("clm-mig-g3", "MIG-G", "marker", "MIG-P-g")
    verdict("vrd-mig-g1", "MIG-G", "marker", "approved", [first], 1)
    verdict("vrd-mig-g2", "MIG-G", "marker", "approved", [reasserted], 2)

    questions = [("marker", s, "segmentation_role")
                 for s in ("MIG-A", "MIG-B", "MIG-C", "MIG-D", "MIG-E", "MIG-F", "MIG-G")]
    before = {q: _old_answer(old, *q) for q in questions}
    old_claims = set(ClaimEvent.objects.values_list("uid", flat=True))

    call_command("migrate", "clinicalomx_ledger", verbosity=0, interactive=False)

    migrated = Claim.objects.filter(uid__in=old_claims)
    assert set(migrated.values_list("uid", flat=True)) == old_claims
    assert set(migrated.values_list("schema_version", flat=True)) == {1}
    assert Claim.objects.get(uid="clm-mig-a").run_uid == "run-mig-1"
    after = {q: [d.claim.value for d in services.answers(*q)] for q in questions}
    assert after == before
    assert before[("marker", "MIG-B", "segmentation_role")] == ["cyto_structural"]
    assert before[("marker", "MIG-F", "segmentation_role")] == ["nucleus"]
    assert before[("marker", "MIG-G", "segmentation_role")] == ["marker"]

    queued = {c.subject for c in services.queue("marker")}
    assert {"MIG-D", "MIG-E"} <= queued, "undecided and retracted claims are the queue"
    assert Decision.objects.get(uid="vrd-mig-a").outcome == "accepted"
    assert not Decision.objects.filter(uid__startswith="vrd-mig-d").exists(), (
        "a proposed verdict writes no decision"
    )
    assert Claim.objects.get(uid="clm-vrd-mig-f").source == "human", (
        "a verdict citing no claim of its value is backed by the decider's own"
    )
