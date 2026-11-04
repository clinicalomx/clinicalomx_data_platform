"""Hand-authored append-only trigger DDL, the gate-14 red-team rehearsal form.

One self-contained CREATE FUNCTION plus, per event table, a row-level
BEFORE UPDATE OR DELETE trigger and a statement-level BEFORE TRUNCATE trigger
(TRUNCATE fires no row-level triggers, so it needs its own). The refusal binds
every SQL client including a superuser; break-glass is explicit owner-only
ALTER TABLE ... DISABLE TRIGGER USER (docs/runbook.md).

pgtrigger was deliberately dropped (ADR decision, 2026-08-28): it is effectively
single-maintainer, and a table-scoped pg_dump silently omits its scattered
17-function web, restoring data with ZERO triggers. This form is one versioned
function the exit runbook re-applies on any restore target; this migration file
is the canonical DDL artifact.

The read model (clinicalomx_ledger_resolvedstate) is deliberately NOT protected:
it is the mutable CQRS projection, rebuilt from events.
"""

from django.db import migrations

EVENT_TABLES = (
    "clinicalomx_ledger_claimevent",
    "clinicalomx_ledger_verdictevent",
    "clinicalomx_ledger_verdictsupport",
    "clinicalomx_ledger_retractionevent",
    "clinicalomx_ledger_sourceinvalidationevent",
)

CREATE_FUNCTION = """\
CREATE FUNCTION ledger_refuse_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'clinicalomx_ledger: % is append-only; % refused',
        TG_TABLE_NAME, TG_OP;
END;
$$;
"""

CREATE_TRIGGERS = "\n".join(
    f"""\
CREATE TRIGGER ledger_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION ledger_refuse_mutation();
CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_refuse_mutation();"""
    for table in EVENT_TABLES
)

APPEND_ONLY_SQL = CREATE_FUNCTION + "\n" + CREATE_TRIGGERS

DROP_SQL = (
    "\n".join(
        f"""\
DROP TRIGGER IF EXISTS ledger_append_only ON {table};
DROP TRIGGER IF EXISTS ledger_no_truncate ON {table};"""
        for table in EVENT_TABLES
    )
    + "\nDROP FUNCTION IF EXISTS ledger_refuse_mutation();"
)


class Migration(migrations.Migration):
    dependencies = [
        ("clinicalomx_ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=APPEND_ONLY_SQL, reverse_sql=DROP_SQL),
    ]
