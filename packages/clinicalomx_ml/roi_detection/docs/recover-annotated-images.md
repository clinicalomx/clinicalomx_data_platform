# Recover `clinical-roi/core/annotated_images`

Rebuild the `clinical-roi/core/annotated_images` Collection when it is missing, so the `core` training chain can run again.
Run every step yourself, in order, and stop at the first STOP.

## When you need this

`python -m croi tma_core ingest-labels` reads its images from `clinical-roi/core/annotated_images` version `"1"`, and no code in this repository creates that Collection.
If it is gone, the core chain stops at its first step.
`utils-reset-all-roi` once deleted it; that flow no longer deletes another Storage's Collections.

The `utils-recover-annotated-images` deployment rebuilds it from lineage: the run that registered each `core_labels/` label names the image it was drawn on.
After that, the two existing stages run unchanged.

A croi image older than `clinical-roi` 0.3.2 spells each stage `python -m core <stage>`.
On one of those, read `python -m croi tma_core` below as `python -m core`.

## What writes

- A run without `apply=true` is the check. It writes nothing: no record, no Run, no file. Run it as often as you like.
- A run with `apply=true` is the only step of this tool that writes.
- Steps 3 and 4 are the existing stages and write what they always write.

## Where to run it

Steps 1 and 2 are runs of the `utils-recover-annotated-images` deployment, submitted from the operator workstation over the tunnel.
The deployment runs on `hpc-ssh-pool` in the `croi-core-train` SIF and prints its output to the run logs.
Read the verdict there:

```bash
# operator workstation
DEP=recover-annotated-images/utils-recover-annotated-images
prefect deployment run "$DEP" --watch
prefect flow-run logs <flow-run-id>
```

A verdict that is not `CLEAR` fails the run, with the verdict line as the failure message.

Steps 3 and 4 are the existing `ingest-labels-core` and `split-core` deployments, submitted the same way.
Each ends on a verdict line, `WROTE:` or `UNCHANGED:`.

Step 5, and the local path for every step, need an HPC node that mounts `storage.model_root` from `site.yaml`.
Run inside the deployed core train image, with your lamin credentials in `$HOME` (Apptainer binds it by default):

```bash
# HPC node
SIF=<storage.hpc_alias>/environments/croi-core-train_v0.3.5.sif
TOOL=<path to this repository on the HPC>/packages/clinicalomx_ml/roi_detection/src/croi/objectives/tma_core/recover_annotated_images.py
```

**Local path, without the tunnel.** Each deployment run below shows its script form under it.
Run the script as a file: `ln.track()` refuses a stdin heredoc.

## Steps

### 1. Check (writes nothing)

```bash
prefect deployment run "$DEP" --watch
# local path: apptainer exec "$SIF" python "$TOOL" check
```

It prints one line per precondition (PASS, FAIL or WARN), then one verdict line.

| Check | PASS means | A FAIL means |
|---|---|---|
| P1 | the Collection exists and holds exactly the images the labels name | before step 2 this is expected and is the only FAIL allowed |
| P2 | every `core_labels/` record and every named image has its file | ingest-labels permanently deletes a label record whose file is gone |
| **P3** | the `.txt` files on disk are exactly the label records | **a file with no record is newly registered and silently joins the dataset; a record with no file is silently left out** |
| P4 | this node mounts the croi storage root | you are on the wrong node |
| P5 | no other Collection holds the members `clinical-roi/core` will hold | lamindb would re-key that Collection to `clinical-roi/core` |
| **P6** | every image still points at its own ROI Record, or at none | **WARN: labels will attach to Records matched by name alone, and a re-minted pipeline TMA can carry the name of a different physical TMA; the verdict waits on `--accept-by-name-links`. FAIL: two Records share one name, ingest-labels aborts partway, and the flag does not override it** |
| P7 | each label's run names exactly one image by ingest's own name rule | the lineage cannot say which image a label belongs to; `apply` refuses |

**STOP** if the last line starts `STOP:` (the run fails; the script exits 1).

P6's title counts three groups: images that keep a live ROI, images that resolve by name alone, and images with no ROI.
A PASS over images with no ROI is not a safe instance: ingest links none of them.
The line under P6 splits them into dangling pointers and no pointer at all.
It does not block, because split and train read no ROI link.

If P6 is WARN, the last line starts `ACKNOWLEDGE:` (the run fails; the script exits 1).
P6 lists each image, the Record its name resolves to, that Record's type and its creation date.
Linking the labels to those Records is your decision, not the tool's.
To accept them, re-run with the flag:

```bash
prefect deployment run "$DEP" --param accept_by_name_links=true --watch
# local path: apptainer exec "$SIF" python "$TOOL" check --accept-by-name-links
```

P6 still prints WARN and lists every link.
Only the verdict changes.

The verdict you want: ``CLEAR: next step is `apply`.``

### 2. Apply (writes one Collection)

```bash
prefect deployment run "$DEP" --param apply=true --watch
# add --param accept_by_name_links=true only if you accepted P6 in step 1
# local path: apptainer exec "$SIF" python "$TOOL" apply
```

The deployment repeats step 1's checks first and writes only on a `CLEAR` verdict.
So an apply over a P6 WARN without `accept_by_name_links=true` fails and writes nothing.
The script's `apply` does not repeat the checks: on the local path, run step 1 first.

It creates `clinical-roi/core/annotated_images` version `"1"`, plus one Transform and one Run for the tool.
No artifact is created, changed or moved.

Expect `saved clinical-roi/core/annotated_images v1 uid=... with N images for M labels`.

If a Collection with that key already exists, it prints `already exists; nothing written` and writes nothing, not even a Run.
Running it twice is safe.

**STOP** if it prints `refusing:`.
That is P7, and nothing was written.

The run then repeats the checks under `checks after apply:`.
On the local path, re-run step 1, with `--accept-by-name-links` if you accepted P6.
The verdict you want: `CLEAR: the collection is in place. Next: python -m croi tma_core ingest-labels.`
Every line must PASS, except a P6 WARN you accepted.

### 3. Ingest labels (existing stage)

```bash
prefect deployment run core-ingest-labels/ingest-labels-core --watch
# local path: apptainer exec "$SIF" python -m croi tma_core ingest-labels
```

It creates one Collection, `clinical-roi/core`, from each matched image and its label.
Every label prints `skip compute (exists)`: no label artifact is created.
It writes an ROI link per label, which is what P6 described.

**STOP** before step 4 if:

- `Matched N (image, label) pairs.` does not equal the label count P7 printed;
- any `WARN: ... labels had no matching image` line appears.

### 4. Split (existing stage)

```bash
prefect deployment run core-split/split-core --watch
# local path: apptainer exec "$SIF" python -m croi tma_core split
```

It creates `clinical-roi/core/splits/{train,val,test}`, version 1 each, and no artifacts.
Seed and fractions are unchanged, so each slide lands in the split it had before.

**STOP** on `Empty train split`.

### 5. Verify (writes nothing)

```bash
# HPC node
apptainer exec "$SIF" python -m croi tma_core validate
```

It reports dataset findings without training.
The three split counts must add up to `clinical-roi/core`'s count.

Re-running step 3 or 4 with nothing changed creates no new Collection version.

## Tests

`tests_pg/test_recover_annotated_images.py` runs the tool against a disposable Postgres-backed instance.
It covers check, apply, a second apply that writes nothing, the real `ingest-labels` flow on the result, and each precondition failing.
It also runs the deployment as a real Prefect flow run, checks both parameters default to off, and runs `ingest-labels-core` and `split-core` twice each (`WROTE:`, then `UNCHANGED:`).

From `packages/clinicalomx_ml`:

```bash
uv pip install --python .venv/bin/python pytest "testcontainers[postgres]"
.venv/bin/python -m pytest roi_detection/tests_pg -q
```
