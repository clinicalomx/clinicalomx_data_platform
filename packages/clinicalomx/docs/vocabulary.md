# The vocabulary

The types, features and storage locations the instance must contain, kept as data files in `src/clinicalomx/vocab/data/` and applied by a seeder.
A seeder is a script that creates whatever the data files declare and the instance lacks.
[reference.md](reference.md) is the generated list of what is declared today.

## What is declared here

This package declares the **foundation**: the names the framework itself reads or writes, plus the shared conventions no single repo owns.
That is 8 types and 20 active features plus 1 deprecated.

Every other name belongs to the repo that uses it, declared with `clinicalomx.vocab.Vocabulary` and the `clinicalomx.vocabulary` entry point.
`bgnorm_*` and `qptiff_*` belong to the pipeline, detection geometry to clinical-roi, the clinical covariates to the patient ETL.
[extending.md](extending.md) shows how to declare one; [migration.md](migration.md) has the ownership map.

To decide where a new name goes, ask: would a brand-new clinicalomx instance be broken without it?
If yes, it is foundation.
If no, it belongs in the repo that writes it.

| File | Declares |
|---|---|
| `features.csv` | The foundation features. The contract. |
| `types.yaml` | Type records (`is_type=True`), with `uid` as a drift tripwire. |
| `storages.yaml` | Storage roots per environment, selected by `--default-storage`. |
| `sources.lock.yaml` | Pinned public-ontology versions. A lockfile, and empty on purpose. |

## Commands

Connect to the instance first; `apply` never calls `lamin connect` for you.
Only `docs` works without a connection.

```bash
python -m clinicalomx.vocab plan     # what would be added to reach the floor, no writes
python -m clinicalomx.vocab apply    # add whatever the floor is missing
python -m clinicalomx.vocab apply --default-storage hpc_file
python -m clinicalomx.vocab check    # CI gate, non-zero if the floor is unmet or has drifted
python -m clinicalomx.vocab audit    # MISSING (fails --strict), extra, and who owns what
python -m clinicalomx.vocab audit --strict                    # undeclared names, and owners
python -m clinicalomx.vocab apply --with-consumers   # provision a fresh instance end to end
python -m clinicalomx.vocab docs                     # render the reference (no database)
```

`--with-consumers` widens `plan` and `apply` to every installed consumer's `Vocabulary`.
Plain `apply` touches only the foundation.
In `clinicalomxdb` these are wrapped as `make schema-plan` and friends.

Every command only adds.
None removes anything, and none objects to a name it did not declare.

## Reading `audit`

```text
MISSING   declared but absent    the floor is broken   -> fails --strict
extra     present, undeclared    the flexibility       -> reported, never fails
```

Read the extras for near-duplicates of a declared name, such as `patientid` beside `patient_id`, or `Source` beside `source_feature`.
Promote the ones that are real; leave the rest.
`(unowned)` marks a name nobody declares.

## features.csv

```text
name,dtype,description,coerce,status
```

`status` is `active` or `deprecated`.
Deprecated rows are kept and never re-seeded.
Declare a feature before anything writes to it.

dtype spellings:

```text
str | int | float | bool | datetime | date | num
Artifact | Record | Collection          bare registry name
cat[Artifact] | cat[Collection]         lamindb's serialization; both accepted
cat[bionty.Protein]                     dotted names resolve through the module
type:<TypeName>                         categorical pinned to a types.yaml entry
```

Use `type:<Name>` for categoricals.
Never hand-write `cat[Record[<uid>]]`.

## types.yaml

Each entry has `name`, an observed `uid`, an optional parent `type`, and a description.
The `uid` is a tripwire and is never written back.
`s0002` reports a changed UID as UID DRIFT: the type was deleted and recreated.

`SeedVersion` is not in the file; the ledger creates it at apply time.
So the generated reference lists nine types where the file declares eight.

## Adopt an instance

Do this for a new instance or a re-adoption.

1. Print a summary. This writes nothing.

   ```bash
   python scripts/dump_instance.py
   ```

2. Overwrite `types.yaml` and `features.csv` with what the instance holds.

   ```bash
   python scripts/dump_instance.py --write
   ```

3. Make `steps/` declare exactly the dump, and run `plan` until nothing is pending.
4. Commit that as the baseline.
5. Fix each bug the dump surfaced as its own change.

## Rules

- **Steps are append-only.** Never reorder or renumber a step that has run.
- **Ontology imports are pinned.** `data/sources.lock.yaml` is `sources: {}`, so `s0004_bionty` is a no-op. Never float a source to `latest-version`.
- **No LLM runs in a seed.** An agent that proposes rows opens a pull request; it does not run at apply time.

## When repair becomes unsafe

`upsert.feature()` repairs a drifted dtype in place.
After the baseline commit, never change a dtype in place: records already holding values under the old dtype are corrupted, and nothing errors.

To change a dtype:

1. Add a new feature name with the new dtype.
2. Mark the old one `deprecated` in `features.csv`.
3. Backfill.

`slide_feature` is the standing example of the deprecated half.

## Deploy order

```text
tofu apply                        infra
django migrations                 only if custom models ever appear
clinicalomx.vocab apply           vocabulary  <- here
pipeline flows                    application data
```

`clinicalomxdb` operates the instance and wraps these commands as `make schema-*`; it declares nothing of its own.
The code is in [`src/clinicalomx/vocab/`](../src/clinicalomx/vocab/).
