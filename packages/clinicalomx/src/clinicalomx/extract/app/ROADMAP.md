# Extract Explorer roadmap

Parked work with no owner.
It lists what the Extract Explorer and the extract layer lack as a data mart builder, three known defects, and the order to build the rest.

A data mart builder here selects a set of ROIs, builds a durable product from whatever the instance holds, and records what built it.

## Constraints on any new work

- An analysis-grade ROI carries at most one patient. An ROI holding several patients is a container for other ROIs.
- A patient-keyed table must REFUSE a container row, never stringify the union of its patients.
- A flat analysis table carries its grouping keys, so rows that share a patient stay identifiable.

## What exists

| Capability | State | Where |
|---|---|---|
| Report seven kinds of absence, per ROI | Exists | `availability.py` |
| Never claim "0 missing" without probing | Exists | `test_web.py` |
| Per-target degradation, not per element | Exists | `availability.py` |
| Instance-wide survey, grouped | Exists | `corpus.py`, `web/state.py` |
| Per-slot element choice, pin or Auto | Exists | `web/state.py` |
| Sub-images of one store chosen separately | Exists | `availability.py:addressable`, `web/state.py` |
| Add a slot from anything addressable | Exists | `web/state.py`, `request.py` |
| Blueprint drawn from the request | Exists | `web/state.py`, `app/diagram.py` |
| Content-hashed plan, stale refusal | Exists | `plan.py`, `assemble.py` |
| Governance verdict on every output key | Exists | `plan.py`, `publish.py` |
| Relations recorded, gaps left null | Exists | `relations.py` |
| Size ceiling on the unbounded product | Exists, build-time only | `mosaic.check_footprint` |
| Population by containment | Exists in the layer, not on screen | `select.py` |
| Population by predicate | Partial: layer only, database only | `request.py`, `select.py` |
| Many plans under one intent, costed, resumable | Partial: agent surface only | `jobs.py`, `tools.py` |
| Cost stated before consent | Partial: agent surface only | `tools.py` |
| Element count for a merged product | Absent | `plan.py` |
| Product carries its request or token | Absent | `publish.py` |
| Product identity independent of clock and path | Absent | `plan.py` |
| Find an existing product before building | Absent | nothing |
| Record what a partial build omitted | Absent | `spatialdata.py` returns a string |
| Patient or covariate axis in any product | Absent | `recipes.py` |
| A flat analysis table carrying grouping keys | Absent | no such target |
| `Destination.overwrite` honoured | Absent, declared | `request.py` |
| `ExtractRequest.label` used | Absent, declared | `request.py` |

## Gaps

1. **The screen cannot select a population.** The Build tab builds for the one ROI in the sidebar, and `plan()` refuses more than one root. `RoiQuery` and `jobs.plan_dataset` handle many ROIs, but only `tools.py` calls them. The CLI has no dataset subcommand.
2. **Selection by predicate needs the database.** The `query` branch of `select()` always calls `narrow()`, which uses the lamindb `Source`. Selection by containment works offline; selection by predicate does not. A Snapshot-backed `Source` would fix it; none exists.
3. **No product has a patient axis.** Every foundation recipe is an image or geometry container. The demo fixture writes no `patient_id` or `cohort`, so `survey(group_by="patient")` over the demo returns one group, `(none)`.
4. **A product does not say which request built it.** `register_outputs` writes an ROI link and `COORDINATE_SPACE`. The plan token reaches no artifact, feature or file.
5. **The token names a planning event, not a product.** `Plan.token` hashes `index_built_at` and every `Unit.output` path, so the same request a minute later, or to another directory, gets a different token.
6. **The Explorer can show one plan and build another.** See defect D1.
7. **Nothing looks for an existing product before building.** `idempotency.materialize` skips by OUTPUT PATH only.
8. **Cost never reaches the screen.** `Job.estimate()` and `job_caveats` exist for the agent. The Build tab shows a unit count only, and `mosaic.check_footprint` refuses only at build time.
9. **A merged plan does not say how big it is.** `Plan.summarise` never prints `Unit.child_elements`, and it lists children-level elements as if the anchor supplied them.
10. **A partial product does not record what it omitted.** `build_merged` returns its `skipped` list inside a detail string that nothing keeps.

## Defects

**D1. The Explorer can assemble a product other than the one on screen.**
The session plan is global and survives a change of ROI or product, so the Assemble button builds the stale plan against a live instance.

**D2. `clinicalomx-extract assemble --plan` refuses every time.**
`cmd_assemble` re-plans from the stored request and compares tokens.
`Plan.token` hashes `index_built_at`, which a re-plan takes from `_now()` at second resolution.
The tokens differ unless the re-plan lands in the same second.

**D3. `Destination.overwrite` is declared and never read.**
`builders/spatialdata._write` deletes an existing output unconditionally.

## Do not

- Auto-fill an empty slot from discovery. The bgnormed zarr's `labels` variable is a GMM component map, not a segmentation.
- Widen `plan()` to accept many ROIs. Put a Job in the UI instead.
- Build the analysis table by joining inside a builder. The grouping keys are part of the product.
- Classify a `.zarr` by its suffix. `builders/kinds.py` omits `.zarr` on purpose.
- Replace the Explorer's explicit cache generation with a TTL.
- Spread writes into the builders. `publish.py` is the one module the read-only AST guard lets write.

## Order of work

| # | Work | Unblocks | Cost |
|---|---|---|---|
| 1 | Clear the plan when the ROI, product or element selection changes (D1) | everything after it | small |
| 2 | Fix `assemble --plan`: compare a token without `index_built_at`, and keep the timestamp as the separate staleness check (D2) | 3 and 5 | small, after open question 1 |
| 3 | Download and upload an `ExtractRequest` as YAML (`to_yaml`, `from_yaml`) in the Explorer | re-runnable, reviewable sessions | small |
| 4 | Stamp the request, recipe version and omissions onto the product, in `register_outputs` and the sidecar | 8 and 9 | medium |
| 5 | Add a Build-tab mode and a `dataset` CLI subcommand over `jobs.plan_dataset`, showing `Job.estimate()` first | gap 1 | medium |
| 6 | Serve `RoiQuery` tier-1 predicates from the Snapshot when one is supplied; do this after 5 | gap 2 | medium |
| 7 | Print `Unit.child_elements`, estimate the mosaic footprint at plan time, and show cost on the Build tab | gaps 8 and 9 | small to medium |
| 8 | Write `build_merged`'s `skipped` list into the block from 4 | gap 10 | small |
| 9 | Look for an existing product by the identity from 4 before building | gap 7 | medium |
| 10 | A recipe whose rows are analysis-grade ROIs carrying grouping keys, refusing container rows; start only once patient curation exists | gap 3 | large |
| 11 | Honour `Destination.overwrite` (D3) and use `ExtractRequest.label` | safe re-runs, named products | small |

## Open questions

Each has a recommendation; none is decided.

1. **One token or two?** Recommendation: two, a plan token that keeps the timestamp and a product identity over the request and resolved inputs only.
2. **Product identity as an Artifact feature, a sidecar, or both?** Recommendation: both, sidecar first, as `relations.py` does.
3. **May a partial product register as an Artifact?** Recommendation: yes, once item 8 records what it omitted.
4. **Does the analysis table belong in `clinicalomx.extract` or in the patient-curation package?** Recommendation: extract, with the refusal delegated to the curation package's read contract.
