# Shared machinery moved into `clinicalomx` v0.2.0

**Done, except two live checks:** [Still to run](#still-to-run).
This page says where the moved code lives now, what this repo kept, one design decision, and the traps found on the way.

## Where things live now

| What | Now in `clinicalomx` |
|---|---|
| Role-feature readers | `clinicalomx/artifacts.py`: `as_artifact`, `all_artifacts`, `newest_artifact` |
| Idempotent `annotate` | a guard in `clinicalomx/roi.py` (`registry_helpers.anchor` is deleted) |
| `roi_id` / `geometry_row` | `clinicalomx/geometry.py`, reusing `FEATURES.ROI_ID` |
| Check machinery and ROI invariants | `clinicalomx/checks/`, the `clinicalomx.checks` entry point, and its `run` / `list` CLI |
| Governance `Policy` | `clinicalomx/governance.py`, the `clinicalomx.governance` entry point |

This repo keeps `checks/pipeline.py`, `governance.POLICIES`, `keys.py`, `contexts.py`, `context_io.py`, `roi_io.write/read_roi_parquet`, the state vocabulary, and all flows.

Role features are DISCOVERED from each consumer's declared `Vocabulary`, not from a hardcoded list.
So croi's `detections` role feature, which the old list missed, is checked too.

## After bumping `clinicalomx`

1. Rebuild and re-tag every image whose wheels embed the framework: `croi-general-inference`, `croi-core-inference`, `bgnorm-inference`.
2. Bump `sif_artifact` in `prefect.yaml`.
   Without a new tag, the HPC worker reuses the stale SIF, because SIFs are cached BY PATH.
3. Run `python -m clinicalomx.vocab apply`.
   The persist stages no longer create a missing foundation feature as a side effect, so this is a prerequisite.
   The `vocab.declared_names_are_present` check reports a gap.

`docs/DEPLOYING.md` is the full procedure.

## Still to run

1. **The forward accept path.** Take one `general_pending` slide through accept, general-persist, core-detect, accept, and core-persist. The re-run below covered only the cached and reconcile half, not a fresh persist.
2. **The four storage-backed checks.** They run only with `include_storage=True`:

   ```bash
   prefect deployment run data-tests/pipeline-data-tests -p fail_on_error=false
   ```

## What was verified live

- `python -m clinicalomx.checks run`: 0 errors, 0 warnings. The metadata checks pass, and the 4 storage-backed ones skip correctly without byte access. The framework's invariants and this repo's were discovered through the entry point, not imported.
- `python -m clinicalomx.vocab audit`: 0 missing, and the newly declared names (`tma`, `parquet_row`, `row`, `col`, `is_detected`, `core_completeness`) are attributed rather than `(unowned)`.
- Re-running `general-detect` on a slide already past general review, on the HPC:

| Claim | Evidence |
|---|---|
| `materialize`'s cached path | `skip compute (exists)` on all three model-versioned keys |
| the state guard | `leaving state general_approved untouched (already past general review)` |
| reconcile runs on the cached pass | logged `1 ROI(s) (1 TMA)` *after* compute was skipped |
| `annotate` is idempotent | role features stayed at n=2, not 3 |

**A role feature at n=2 on an older slide is not duplication.**
The older artifacts sit at `predictions/general/...` and the new ones at `predictions/general-<model_uid>/...`, so they are genuinely different artifacts.
`newest_artifact` orders by `created_at`, so readers get the model-versioned one.
Only a second re-run, once the model-versioned artifacts existed, asked `annotate` to re-link an identical `(roi, feature, artifact)`, and that held the count.

## Decided against: `ln.Schema` on the ROI types

Checked against the lamindb 2.8.1 source.
`Record.schema` is a real foreign key (`record.py:771`) and `add_values` enforces it (`_feature_manager.py:1668-1685`), so write-time validation would work through the path `annotate` already uses.
It was still rejected:

- **Attaching a schema forces an allow-list.** Any feature outside `schema.members` is rejected, and neither `maximal_set` nor `flexible` is consulted on the Record path. That is closed-world, while a consumer producing a new kind of artifact is open-world. It also contradicts `vocab audit`'s own stance that an undeclared name "is not a problem, this is the flexibility".
- **`minimal_set` cannot replace the presence checks.** It validates each `add_values` CALL against only that call's keys (`curators/core.py:611-634` builds a one-row frame from the passed dict and never reads the record). An ROI Record is assembled across several calls: `get_or_create`, then `create`'s `parent_roi` upsert, then `annotate`.
- **Attaching to a live type is strict backwards.** One missing member breaks detect, persist and bgnorm on their next write.

Role-feature discovery uses the `clinicalomx.vocabulary` entry point instead, and completeness stays with the read-only checks.

## Open

`roi_id_for` has no callers.
It is the convention for turning a name into an identity, but the two persist sites build the column with inline f-strings.
So "the two cannot drift" is a convention, not shared code.

## Connecting from a workstation

The framework repo's own environment has `lamindb` as a hard dependency, so `uv run` there provides both it and the `lamin` CLI.
Two traps:

- **`lamin connect` rewrites the host.** It changes the host in `~/.lamin/current_instance.env` from `localhost` to the instance's public address, which bypasses an SSH tunnel. Set the host back after every connect.
- **A local Postgres on port 5432 hijacks the tunnel.** If a local Postgres holds `127.0.0.1:5432`, `ssh -L 5432:...` binds IPv6 only and silently fails on IPv4 (`bind: Address already in use`). `localhost` then reaches the LOCAL server, which has no `<instance>_jwt` role, and Postgres reports a missing role as "password authentication failed". That reads exactly like a bad credential. Check which process owns each line; both should say `ssh`:

  ```bash
  ss -ltnp | grep 5432
  ```
