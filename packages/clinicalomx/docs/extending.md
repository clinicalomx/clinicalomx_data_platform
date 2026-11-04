# Extending clinicalomx from your own repo

Declare your own vocabulary, checks, governance policies and recipes in your repo, and clinicalomx finds them once your package is installed.
It finds them through entry points: names a package registers in its `pyproject.toml` so other installed code can look them up.

| You want | Entry-point group | What the entry point must point at |
|---|---|---|
| a feature or record type of your own | `clinicalomx.vocabulary` | a `clinicalomx.vocab.Vocabulary` instance |
| an invariant checked over the instance | `clinicalomx.checks` | a CALLABLE returning an iterable of `Check` |
| a data-governance policy for a folder | `clinicalomx.governance` | a `tuple[Policy, ...]`, or a callable returning one |
| a downstream product an ROI can become | `clinicalomx.recipes` | an ITERABLE of `Recipe` (not called) |

The checks group is CALLED; the recipes group is iterated.

## Where a name belongs

Ask: would a brand-new clinicalomx instance be broken without this name?

- Yes: it lives in the foundation, like `roi_uid`.
- No: it lives in the repo that writes it, like `bgnorm_attempts`.

Almost every new name is the second kind.

## Declare vocabulary

1. Declare the names in one module:

   ```python
   # myrepo/vocab.py - one module, so a rename is one diff
   import lamindb as ln
   from clinicalomx.vocab import Vocabulary

   VOCAB = Vocabulary("myrepo")

   # feature() returns the name, so the declaration IS the constant
   BGNORM_ATTEMPTS = VOCAB.feature("bgnorm_attempts", int,
                                   "Runner launches of bgnorm-core for this core.")
   BGNORM_QC = VOCAB.feature("bgnorm_qc", ln.Artifact, "Per-core QC metrics parquet.")
   MY_KIND = VOCAB.type("MyRoiKind", "What it is.", parent="ROI")
   ```

2. Register the entry point:

   ```toml
   # myrepo/pyproject.toml
   [project.entry-points."clinicalomx.vocabulary"]
   myrepo = "myrepo.vocab:VOCAB"
   ```

3. Call `VOCAB.ensure()` at stage startup. It creates the names idempotently and returns the same `Reconciler` that `plan` reports on.

## Declare checks

1. Write each check as `fn(snap: Snapshot) -> list[str]`, returning one string per failing instance. Read only the `Snapshot` you are handed; never the registries, and never write.
2. For a check over a NON-ROI record type, call `snap.load_values(type_name)` first.
3. Register the entry point:

   ```toml
   [project.entry-points."clinicalomx.checks"]
   myrepo = "myrepo.checks:my_checks"        # () -> list[clinicalomx.checks.Check]
   ```

`clinicalomx-checks run` then runs the framework's checks plus yours, with one exit code.

## Declare governance policies

1. Register the entry point:

   ```toml
   [project.entry-points."clinicalomx.governance"]
   myrepo = "myrepo.governance:POLICIES"     # tuple[Policy, ...], or a callable returning one
   ```

2. Declare one `Policy` per folder. The four flags form a ladder, weakest first, and each requires the one below it:

   ```python
   import clinicalomx as cx

   cx.Policy(folder="lab-internal", readable=True, ingest=True, derive=True, training=False,
             basis="HREC 2024/001")

   cx.Policy(folder="bad", readable=False, ingest=True, derive=False, training=False)
   # ValueError: 'bad': ingest requires readable (registering reads the bytes)
   ```

Governance denies by default: an undeclared folder resolves to `UNDECLARED`, which permits nothing.
Two consumers declaring one folder with different terms raises an error.

`Policy` and `permits_*` are what you declare against here.
Claims and decisions live in a separate package, `packages/clinicalomx_ledger`, read through `clinicalomx_ledger.reads`.
Read the platform ADR `docs/architecture/decisions/2026-08-28-governance-placement.md` before building anything larger on either.

## Declare recipes

```toml
[project.entry-points."clinicalomx.recipes"]
myrepo = "myrepo.recipes:MY_RECIPES"      # an iterable of clinicalomx.extract.Recipe
```

A consumer recipe replaces a foundation recipe of the same name.
`docs/extract-recipes.md` lists foundation recipes only.
`clinicalomx-extract recipes` shows what your environment has.

## Reinstall after adding an entry point

An entry point comes from INSTALLED metadata, not from source.
Code that arrives by `git clone` contributes nothing.

1. After adding or changing an entry point, reinstall:

   ```bash
   uv pip install --no-deps -e .
   ```

2. Where you cannot install, register at runtime instead:

   ```python
   cx.register_policies(POLICIES)                       # governance
   clinicalomx.extract.register_recipes(MY_RECIPES)     # recipes
   ```

   Registering identical terms is a no-op, so call `register_policies` unconditionally.
   Checks have no runtime hook; the caller names them explicitly.
   Vocabulary needs none: `VOCAB.ensure()` calls your `Vocabulary` directly.

## Verify it worked

```bash
uv run python -m clinicalomx.checks list      # your checks appear, grouped under your name
uv run python -m clinicalomx.extract recipes  # the provider count includes you
uv run python -m clinicalomx.vocab audit      # your names attributed to you, not "(unowned)"
```

`list` and `recipes` need no database.
`audit` needs a connected instance.
If your owner name vanishes from `audit`, your package's installed metadata is stale: reinstall it.
