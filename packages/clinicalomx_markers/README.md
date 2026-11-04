# clinicalomx_markers

The lab's marker registry.
This details which analyte a raw channel name means, the human or scientist approved definition/pass, all with integrated logging of decisions made.

It is a plain Python library with no service and no container.
Reading needs no model and no network.

## Depends on

- `clinicalomx`, for the instance vocabulary.
- `clinicalomx_ledger`, for the claim and decision grammar.
- `clinicalomx_reagents`, for the reagent inventory.

All three resolve from their paths in this repository.

## Install

```bash
cd packages/clinicalomx_markers
uv sync                      # the library and the offline test suite
uv sync --extra agent        # add the LLM proposer
uv sync --extra sheet        # add the spreadsheet reader
```

The package has no release tag of its own.
A consumer outside this repository pins a commit.

## Use

```python
from clinicalomx_ledger.grammar.published import load
from clinicalomx_markers.resolve import resolve_all

release = load("marker-rel-....json")       # a release export of accepted aliases
resolved, unresolved = resolve_all(["DAPI", "NakATPase", "Ecad"], release)
```

A name resolves in this order:

1. Lowercase it and drop separators.
2. Match it against `src/clinicalomx_markers/seed_data/markers.csv`, then against the accepted aliases in a release export.
3. Otherwise it waits in the review queue. Nothing guesses.

A **claim** is one assertion plus its source.
Each claim is ruled on by its own append-only **decision**.
The answer to a question is every claim whose newest decision accepts it.
`query.claims.settled` reads accepted answers only.
`query.claims.asserted` reads every standing claim.

`docs/how-to.md` covers every task: resolving names, picking segmentation channels, citing an answer, checking an inventory sheet and recording a decision.

## Layout

```text
src/clinicalomx_markers/
  domain/      the marker, target, channel and reagent-inventory rules. No model, no network
  resolve.py   a raw name to a marker or a platform
  sources/     fetch and keep an upstream record, such as UniProt
  propose/     snapshot to proposed claims. The only place a model runs
  curate/      write curation to the instance
  query/       read settled answers, citations and segmentation roles
  seed_data/   the curated seed CSVs. Their format is in seed_data/README.md
tests/         the offline suite
tests_pg/      the Postgres suite. Needs Docker
```

## Test

```bash
uv run pytest -q
uv run ruff check src tests
uv sync --group pg && uv run pytest tests_pg -q
```

CI runs the first two.
`docs/maintaining.md` explains each suite and how to add a marker, a column, a proposer or a claim domain.

## Status

Every seed row is `status=draft`.
The write path runs against a throwaway instance only: nothing creates a `Marker` on the live instance yet, and the live instance does not carry the ledger module.
