# clinicalomx_ledger

The governance ledger for the clinicalOMx instance: claims, decisions and releases, stored append-only.
Postgres triggers refuse any update or delete of a ledger row.

It is a lamin schema module: a Django app that lamindb installs into the instance database.
It runs on Postgres only.

## Depends on

- `lamindb==2.8.1`, pinned exactly to match every other environment that touches the instance.
- Postgres, for the triggers and the role grants.
- Docker, for the test suite.

## Install on an instance

```bash
lamin init --storage <path> --db <postgres-url> --modules clinicalomx_ledger
lamin migrate deploy
```

Deploying to the live instance is an operator job.
Follow `docs/runbook.md` for role activation, break-glass access, exit and restore.

## Use

A curated domain imports the grammar, which needs no Django and no lamindb:

```python
from clinicalomx_ledger.grammar import example    # the smallest domain that runs
```

A consumer that holds no claim store reads through `clinicalomx_ledger.reads`, which returns dataclass rows.
To add a curated domain, follow `docs/curation-recipe.md`.
`clinicalomx_markers` is the reference consumer.

The answer to a question `(domain, subject, predicate)` is each claim whose newest decision accepts it.
The review queue is the standing claims with no decision, most confident first.
`cut_release` in `services.py` pins a domain's answer, and `export_release` writes it as a file.

## Layout

| Path | Holds |
|---|---|
| `src/clinicalomx_ledger/models.py` | the append-only tables: `Claim`, `Decision`, `Release`, `ReleaseEntry`, `Policy`, `SourceInvalidationEvent` |
| `src/clinicalomx_ledger/services.py` | the writes, the answer and the queue |
| `src/clinicalomx_ledger/reads.py` | the reads for a consumer without a store |
| `src/clinicalomx_ledger/adapter.py` | the store adapter for a consumer's own `ClaimStore` |
| `src/clinicalomx_ledger/grammar/` | the Python API a curated domain imports |
| `src/clinicalomx_ledger/migrations/` | the schema history |
| `disposable.py` | the throwaway instance the tests and `scripts/dev/curation_demo.py` use |

Never squash or rename a migration.
`0005` holds `APPEND_ONLY_SQL`, the trigger DDL the runbook re-applies after a restore.
The distribution, the module, the instance module name and the table prefix `clinicalomx_ledger_*` must stay equal.

## Test

```bash
uv sync
uv run pytest -q
uv run ruff check --no-cache .
```

The suite starts a throwaway `postgres:18` container through testcontainers.
`disposable.py` points `HOME` and `LAMIN_SETTINGS_DIR` at a temporary directory and refuses to run if lamindb was imported first, so it cannot reach a configured instance.
