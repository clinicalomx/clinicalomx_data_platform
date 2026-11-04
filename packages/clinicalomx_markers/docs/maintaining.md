# Maintaining clinicalomx_markers

How to change this package without breaking it.
Read `docs/how-to.md` first if you have not used it.

## Run the tests

The offline suite needs no database, no network and no model:

```bash
uv sync
uv run pytest -q                     # what CI runs
uv run ruff check src tests          # exactly what CI lints
```

On the WSL box `uv run pytest -q` segfaults with exit 139 and no output.
Run `uv run pytest -q -p no:capture` there, and leave the CI command alone.
The cause is `import readline` inside `_pytest/capture.py`, which dumps core on that machine.

The Postgres half is `tests_pg/`, kept out of `testpaths` so the offline suite stays database-free.
It needs Docker:

```bash
uv sync --group pg
uv run pytest tests_pg -q
```

`tests_pg/conftest.py` starts a throwaway `postgres:18` container through testcontainers, runs `lamin init --modules clinicalomx_ledger` against it under a scratch HOME, and runs `LedgerClaimStore` beside `MemoryClaimStore` through one write sequence per rule.
It must be Postgres: lamindb reads differently per database backend, so SQLite proves nothing.
Neither suite touches a live instance, and neither may be made to.

CI is `.github/workflows/pkg-clinicalomx-markers.yml` at the repo root, and it does not run `tests_pg/`.
Each test file's docstring says what it checks.
`test_vocab.py` never calls `VOCAB.ensure()`, so neither suite covers the apply path.

## The shape

A value travels through these directories in order, and each one may do strictly less than the one before:

| Directory | Owns | May not |
|---|---|---|
| `domain/` | the nouns: two claim domains, the reagent inventory and the promoted rules | no model, no network, no database |
| `resolve.py` | a raw name to a catalogue entity, a marker or a platform | no model, no network, no database |
| `sources/` | fetching an upstream authority and keeping the bytes | no model |
| `propose/` | snapshot to proposed claims. The one place a model runs | no database |
| `curate/` | the curation session and its write step, channel records | no model |
| `query/` | settled answers and their citations, segmentation | no model, no network |
| `seed_data/` | the curated CSVs and the reader that validates them | nothing writes them at runtime |

Three top-level modules declare things at deploy time:

- `vocab.py` declares this package's types and features to the instance and re-exports every constant it publishes. Its import block shows which module owns which name. It also defines the retired `MODEL_FEATURES`, which nothing else reads.
- `seed_spec.py` is the column contract for this package's five seed CSVs and their controlled values. The reagent inventory's three are `clinicalomx_reagents.spec`.
- `seed_docs.py` generates `seed_data/README.md` from that contract.

The claim and decision grammar is not here.
It is `clinicalomx_ledger.grammar`, shared by every curated domain, and this package is its reference consumer.

## Rules you must not break

Each has a test standing over it.
Check every change against this list.

1. **The vocabulary import chain reaches no lamindb, bionty, django or psycopg2, and no model client.** Reading a constant must not need a database. `tests/test_boundaries.py::test_the_vocabulary_chain_pulls_in_no_heavy_dependency` walks every link. pydantic and `clinicalomx.vocab` are on the chain on purpose.
2. **No package re-exports anything.** Every `__init__.py` is a docstring only, so a name is imported from the module that defines it. A re-export looks like a caller and keeps dead code alive.
3. **A constant lives in the module that owns it.** `vocab.py` imports FROM those modules, and spells a literal only for a name nothing else reads. Inside the package, import from the owner: going through `vocab.py` pulls in the whole chain.
4. **A claim carries no status.** A claim is what a source SAYS; a decision is what the lab DECIDED about one claim. Both are append-only, so a correction is a later record, never an edit.
5. **A disputable value is a claim. Only structural identity may be a declared field.** Before adding a feature to `vocab.py`, say in the declaration which side of that line it is on.
6. **Renaming a `MarkerPredicate` string orphans every citation written under it,** because the stored value is part of `Claim.claim_id()`. That is why `go_cc` stays spelled `go_cc`.
7. **`ROOT` in `sources/snapshot.py` is what a citation points at.** Changing it breaks every citation pointer. It is safe to change only while nothing has been stored outside a temporary directory.
8. **`sources.uniprot.FIELDS` is everything a claim can ever cite,** because a field never requested is not in the snapshot. To widen it, re-capture the fixtures with `tests/fixtures/recapture_uniprot.py`.
9. **Every agentic step can be done by a human.** For proposing, that is `propose/human.py`: same request in, equal `Proposal` out, same citation rule, same policy. If `curate/` ever needs to tell a human path from a machine one, treat that as a design problem rather than coding around it.

## Add the common things

### A marker, alias or channel row

Edit the CSV under `src/clinicalomx_markers/seed_data/` and run the suite.
`docs/how-to.md` has the user-facing version, including why a redundant alias row fails.

### A column on a seed CSV

Columns are declared in `seed_spec.py`, not in the CSV header, and the reader raises on a file that no longer matches.

1. Edit the `Column` list for that file in `seed_spec.py`.
2. Edit the CSV header and every row.
3. Regenerate the documentation:

   ```bash
   uv run python -m clinicalomx_markers.seed_docs
   ```

If you skip step 3, `tests/test_seed_spec.py` fails on the stale `seed_data/README.md` table.

### A proposer

1. Implement `propose.schema.Proposer`: a `name` attribute and `propose(request: ProposalRequest) -> list[Proposal]`.
2. Emit the whole `Proposal`: the winner's `confidence`, every other value you weighed as ranked `alternatives`, and a `Triage` with a reason (from `schema.assess` unless you know better).
3. Call `domain.rules.check` before returning, so a correction a human made once constrains what you generate, not only what validation catches.
4. Add it to `tests/test_proposal_uncertainty.py`.

- State only numbers you actually have. A model that gave none is `confidence=0.0` and `scrutinise`, never a guess.
- The triage label is about attention and is never a decision.
- When a rule blocks a machine proposer, demote to `SegmentationRole.MARKER`: `marker` is the safe wrong answer, because the channel is then left out of segmentation. `propose/human.py` raises instead, because rewriting a person's typed answer would record something they did not say.
- If your proposer needs a model client, import it inside the function that calls it, as `propose/llm.py` does. Otherwise `tests/test_boundaries.py` fails.

### A transform tool

Do not add one on a guess: vendor formats are unknown and change, and an agent will reach for a bad tool.
A tool earns its place from accepted work.
`transform.usage` counts each accepted chain per sheet, and `Usage.promotion_candidate` marks a bespoke `pattern(...)` accepted on more than one sheet.
Promotion is a reviewed diff, never automatic:

1. Write it as a plain function in `domain/transform.py`, value first, returning `""` where it finds nothing, never the input unchanged.
2. Register it in `TOOLS`. Its type annotations are the argument check, and `str`, `int` and `bool` are the only types a step can carry.
3. Add its cases to `tests/test_transforms.py`, including the cells that made it a candidate.

Existing claims keep their `pattern(...)` value.
A claim's value is its identity, so promotion changes what new proposals can call, not what was accepted.

### A promoted rule

Rules are data in `seed_data/rules.csv`, so adding one is a reviewable diff, not a code change.
`domain/rules.py` reads them, and `MARKER_DOMAIN.policy` is where they reach the grammar.

A rule fires only when the caller hands `validate` the field the rule tests.
`curate.session.record_role` always builds a `subject_row` from the UniProt record, so the curator's path is safe.
Nothing forces the next writer to do the same.

### A claim domain

Follow `packages/clinicalomx_ledger/docs/curation-recipe.md`.
This package is its reference consumer, so a change here that breaks the recipe's rules is a change to both.
`domain/marker.py` and `domain/reagent_mapping.py` are the two worked examples.

A domain owns a source vocabulary, a predicate vocabulary, three models parameterised on them, and one `ClaimDomain` binding them.
Subclass the PARAMETERISED model, `class C(Claim[S, P])`, never `class C[S, P]`, and never re-declare `predicate` in the subclass.

### A declared feature or type

1. Define the name in the module that owns it.
2. Import it into `vocab.py` to declare it. Never spell a literal there.

`tests/test_vocab.py` asserts the declarations and the `FEATURES` class agree both ways.

Renaming a declared feature is a schema migration on a shared instance.
Retiring one takes effect only when an operator runs apply; until then `clinicalomx-vocab audit` reports it as `extra (unowned)`.

## Things that look like bugs and are not

- `test_nothing_is_active_yet` and `test_the_whole_inventory_is_still_draft` are tripwires. Every seed row ships `status=draft`, and these fail the day that changes, so activation is a decision rather than a diff that slips through.
- The read side answers empty, because no claim has an accepted decision yet. `query/segmentation.py` reads roles from a release export (`clinicalomx_ledger.grammar.published`), never from `channels.csv`.
- `test_boundaries.py` runs each check in a subprocess. It has to: it asserts what a fresh interpreter holds in `sys.modules`.
- `reporter_for` and `pairs` belong to `clinicalomx_reagents.inventory` and are tested in that package's `tests/test_inventory.py`. `domain/imaging_channel.py` calls `reporter_for`; nothing here calls `pairs`.

## Where the rest is written

| Question | File |
|---|---|
| what each test file checks | the docstring at the top of each file in `tests/` |
| the seed CSV columns and row counts | `src/clinicalomx_markers/seed_data/README.md` |
| the shared claim and decision grammar | `packages/clinicalomx_ledger/` |
