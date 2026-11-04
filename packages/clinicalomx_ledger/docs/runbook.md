# Operator runbook

Procedures for running the ledger on a live instance database: activating roles, approving policies, break-glass corrections, and restoring from a dump.
Nothing in this package touches a live system on its own; every step here is an operator's.

Words used below:

- **Role**: a Postgres login identity with its own permissions.
- **Trigger**: a database rule that runs on every write. The ledger's triggers refuse UPDATE, DELETE and TRUNCATE.
- **DDL**: SQL that changes the schema, such as `ALTER TABLE`.

## 1. Activate the roles a service uses

Migration `0003` creates three ledger roles that cannot log in and have no password.
`0005` grants them the current tables.

| Role | May |
|---|---|
| `clinicalomx_ledger_agent` | INSERT on claims only, so an agent session cannot write a decision it could label as a human's |
| `clinicalomx_ledger_curator` | INSERT on claims, decisions, policies, releases and release entries, and source invalidations |
| `clinicalomx_ledger_projector` | nothing since `0007`. **Never activate it.** Retiring it is a separate step |

At deploy, activate each role a service actually uses:

```sql
ALTER ROLE clinicalomx_ledger_agent LOGIN PASSWORD '<from the secrets registry>';
```

A credential never goes in a migration or in this repository.
Register the role's name in `infra/secrets/README.md` when the deployment lands.

## 2. Keep writers from owning the tables

`ALTER TABLE ... DISABLE TRIGGER USER` needs table OWNERSHIP, not superuser.

- The migrating identity (the lamindb instance connection that runs `lamin migrate deploy`) owns the ledger tables.
- Never make one of the three writer roles an owner, and never run migrations as one.

That split is what keeps break-glass an explicit operator act that agent code cannot reach.

## 3. Turn on schema-change logging

Run once on the instance database:

```sql
ALTER SYSTEM SET log_statement = 'ddl';
SELECT pg_reload_conf();
```

The default, `none`, logs nothing, so a break-glass change would be invisible.
With `ddl`, every `ALTER TABLE ... DISABLE TRIGGER` lands in the Postgres log.

## 4. Approve a policy an agent decides under

An agent may write a decision only under a policy a human approved.
The decision's `policy_version` is a foreign key to `clinicalomx_ledger_policy`, and a CHECK constraint refuses an agent decision without one.

Approve one as the curator role, naming the approver:

```python
services.approve_policy(version="<name>-v1", approved_by="<human>", note="<what it permits>")
```

A policy is never edited or withdrawn in place.
A changed policy is a new version.

## 5. Break-glass: correct a row by hand

Use this only for a correction the grammar cannot express.
A rejecting decision, a new claim plus a decision, or a source invalidation covers every known case, so prefer those.

1. Check `log_statement` is `ddl` (step 3). Without it there is no audit trail.
2. As the table owner, in one session, with the smallest possible change:

   ```sql
   ALTER TABLE clinicalomx_ledger_claim DISABLE TRIGGER USER;
   -- the mutation, in the same session, smallest possible scope
   ALTER TABLE clinicalomx_ledger_claim ENABLE TRIGGER USER;
   ```

3. Record what was done and why as a ledger row, where possible.

## 6. Restore or export

A plain dump never carries the ledger's guarantees.
A table-scoped `pg_dump` restores the data with ZERO triggers and no error, because the trigger function is a separate object `-t` does not select.
`tests/test_zz_lifecycle.py` is the measured rehearsal.

On ANY restore target:

1. Restore the data.
2. Re-apply the canonical DDL from `migrations/0005_three_table_model.py` (`APPEND_ONLY_SQL`).
   It uses `CREATE OR REPLACE`, so re-applying is safe.
   Do not use `0002`'s copy: it names tables `0007` dropped.
3. Prove UPDATE, DELETE and TRUNCATE are refused before declaring the target live.
4. After a data-only restore, the id sequences lag the restored rows. Reset each table:

   ```sql
   SELECT setval(pg_get_serial_sequence('<table>', 'id'),
                 coalesce((SELECT max(id) FROM <table>), 1));
   ```

## Rolling back the roles

The reverse of `0003` runs `DROP OWNED BY`, which revokes everything granted to the roles in the CURRENT database.

- Keep the roles ledger-only forever. A grant outside the ledger tables would be revoked too by a governance rollback.
- `DROP OWNED BY` does not reach OTHER databases in the cluster. If a ledger dump was restored into a second database, revoke the grants there as well, or `DROP ROLE` refuses.

## The six-table migration (0005 to 0007)

- `0005` creates the three tables beside the old ones, `0006` copies every row across, and `0007` drops the old tables.
- `0006`'s docstring says what each old row became: a claim event is a claim, a decided verdict is a decision per claim it cited, a proposed or retracted verdict writes nothing (its claim returns to the queue), a claim retraction is a rejected decision, and `ResolvedState` is dropped because the answer is now a read.
- `0006` refuses to run on an agent-made retraction, because an agent decision needs an approved policy the old model never recorded. Record that one by hand under an approved policy, or roll back.
- Rolling back past `0007` recreates the old tables EMPTY, protected and granted. It is not a way to recover data; restoring a backup is.
