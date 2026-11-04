"""Roles and grants as a controlled migration, so they survive rollback cycles.

The Option A POC observed that GRANTs issued outside migrations vanish when a
rollback drops and recreates the tables; here they are part of the schema.

Design rules from the accepted ADR (2026-08-28-governance-placement.md):

- Roles are created NOLOGIN with NO password. LOGIN and a password are granted
  by the operator at deploy time (gate 15); a credential never appears in a
  migration.
- Writers never own the tables (rider 1): the migrating identity owns them, so
  owner-only break-glass (ALTER TABLE ... DISABLE TRIGGER USER) is out of every
  writer's reach.
- The roles are LEDGER-DEDICATED and carry the table prefix as a name prefix
  (rider 4): the reverse's DROP OWNED BY is database-global by nature, so the
  only thing keeping a rollback from revoking non-ledger grants is that these
  roles are never granted anything else. Do not reuse them.

No sequence grants: the id columns are identity columns (Django BigAutoField on
Postgres), whose sequences are consulted with the INSERT privilege alone.
"""

from django.db import migrations

EVENT_TABLES = (
    "clinicalomx_ledger_claimevent",
    "clinicalomx_ledger_verdictevent",
    "clinicalomx_ledger_verdictsupport",
    "clinicalomx_ledger_retractionevent",
    "clinicalomx_ledger_sourceinvalidationevent",
)
PROJECTION = "clinicalomx_ledger_resolvedstate"
ROLES = (
    "clinicalomx_ledger_agent",
    "clinicalomx_ledger_curator",
    "clinicalomx_ledger_projector",
)

CREATE_ROLES = "\n".join(
    f"""DO $$ BEGIN
    CREATE ROLE {role} NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;"""
    for role in ROLES
)

ALL_ROLES = ", ".join(ROLES)
ALL_TABLES = ", ".join((*EVENT_TABLES, PROJECTION))

GRANTS = f"""
{CREATE_ROLES}
GRANT USAGE ON SCHEMA public TO {ALL_ROLES};
GRANT SELECT ON {ALL_TABLES} TO {ALL_ROLES};
GRANT INSERT ON clinicalomx_ledger_claimevent
    TO clinicalomx_ledger_agent, clinicalomx_ledger_curator;
GRANT INSERT ON clinicalomx_ledger_verdictevent,
    clinicalomx_ledger_verdictsupport,
    clinicalomx_ledger_retractionevent,
    clinicalomx_ledger_sourceinvalidationevent TO clinicalomx_ledger_curator;
GRANT INSERT, UPDATE, DELETE ON {PROJECTION} TO clinicalomx_ledger_projector;
"""

REVOKES = f"""
DROP OWNED BY {ALL_ROLES};
{"".join(f"DROP ROLE IF EXISTS {role};" for role in ROLES)}
"""


class Migration(migrations.Migration):
    dependencies = [
        ("clinicalomx_ledger", "0002_append_only_triggers"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS, reverse_sql=REVOKES),
    ]
