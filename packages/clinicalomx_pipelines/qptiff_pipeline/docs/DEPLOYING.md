# Deploying a change to the running pipeline

How a change to this pipeline reaches the places that run it, in order, with the traps at each step.
For a full platform release, `docs/operations.md` sections 1 and 2 at the repository root are the procedure.

Four places run this code, and each gets it differently:

| Where | How it gets the code | How it gets `clinicalomx` |
|---|---|---|
| HPC stages | `git_clone` of `main` at run time (`prefect.yaml` `pull:`) | baked into the SIF |
| VM stages + review app | `set_working_directory /home/ubuntu/clinicalomx_pipeline` | the worker's dedicated venv |
| Automations | registered once from `prefect.yaml` deployment ids | n/a |
| The SIFs | pulled from GHCR to scratch, cached BY PATH | it is what is baked in |
| The instance | n/a | vocabulary must be seeded |

- **A flow-code-only change is cheap:** push, then pull on the VM. See [Smaller cases](#smaller-cases).
- **A framework pin change is not:** every image must be rebuilt. There are no compatibility shims, so a SIF built on an older framework fails on import the moment its stage runs.

Words used below:

- **SIF**: the single-file container image Apptainer runs on the HPC. The HPC has no Docker.
- **ORAS artifact**: a file stored in the container registry beside an image. CI publishes each SIF this way.
- **Scratch**: the HPC's shared working storage, where SIFs are kept.

## 1. Push everything

`git_clone` reads `main` from GitHub, so **nothing on the HPC sees an unpushed commit.**
Skipping this step looks like "my change did nothing".

```bash
# your workstation
for r in clinicalomx clinicalomx_pipeline clinicalomx_markers clinicalomxdb \
         clinical_image_roi_object_detection; do
  git -C ~/projects/$r push
done
git -C ~/projects/clinicalomx push --tags
```

## 2. Rebuild the images that embed the framework

Three images carry `clinicalomx` in their wheels.
All three install it from the lab's private index, so each build needs the `INDEX_READ_ACCESS_KEY_ID` / `INDEX_READ_SECRET_ACCESS_KEY` repo secrets (an OpenStack EC2 credential), not a GitHub token.

**croi inference** (`croi-general-inference`, `croi-core-inference`): push a `clinical-roi-v*` tag and the root `img-croi.yml` builds them.
Or start it by hand:

```bash
gh workflow run img-croi.yml -f ref=<image tag>
```

These images resolve the framework from the version RANGE `clinical-roi` declares.
So a `clinicalomx` release does not change croi's image tag by itself: cut a croi tag to force a new SIF path (see step 3).

**bgnorm**: no manual build.
The root `img-bgnorm.yml` runs on any push to `main` that touches `containers/Dockerfile.bgnorm` or the workflow itself.
It derives the tag from that Dockerfile as `<bgnormpy base>-cx<clinicalomx>`, currently `v0.1.2-cx0.1.0`.
So bumping the pin in the Dockerfile rebuilds the image and changes its tag in one push.

- The Dockerfile pins `clinicalomx` to an EXACT version, while `pyproject.toml` declares a range. The tag is derived from the pin, so a range there would let two builds under one tag hold different framework versions.
- The tag names both versions because SIFs are cached BY PATH (`environments/<owner>/<name>_<tag>.sif`). If the tag tracked only the base, a `clinicalomx` bump would republish new contents under the old tag, and every HPC run would keep executing the old SIF.

## 3. Point `prefect.yaml` at the new tags

`prefect.yaml` currently points at `croi-*-inference:v0.3.5` and `bgnorm-inference:v0.1.2-cx0.1.0`.

1. Bump `image:` and `sif_artifact:` together.
   They are two values, one using `:` and one using `_`, so keep the separator and check the diff rather than trusting one pattern:

   ```bash
   sed -i -E 's/(croi-[a-z]+-inference[:_])v0\.3\.4/\1v0.3.5/g' prefect.yaml
   ```

2. Move `packages/clinicalomx_ml/roi_detection/prefect.yaml` in the same change.
   The croi tag is one number across both files, so a sweep over this file alone is not the whole adoption.
3. Commit and push, because the HPC re-clones.

**A `clinicalomx` bump ALONE does not change croi's SIF path**, because croi's images are tagged from croi's own release tag.
A framework release therefore also needs a new croi tag, or every HPC run keeps executing the cached SIF built on the previous framework.
Nothing prompts that tag automatically; it is a step you must remember.

## 4. Fetch the SIFs - REQUIRED after any tag change

Run the `utils-build-environments` deployment once, after the tags move:

```bash
prefect deployment run 'build-environments/utils-build-environments' --param dry_run=true --watch
prefect deployment run 'build-environments/utils-build-environments' --watch
```

- It reads the environments from both `prefect.yaml` files' `sif_artifact` declarations.
- For each published SIF that is missing, it downloads under a lock, checks the layer's sha256, and moves it into place with an atomic rename.
- It does nothing for a SIF already present.
- It refuses an environment that publishes no ORAS SIF. Today that is `bgnorm-inference` alone, which still needs the by-hand step below.
- It refuses a build manifest whose ORAS tag is not the tag the declaration asks for, and reports it as `stale`. A tag you just moved resolves the previous release's manifest until the new manifests are on scratch. Land the manifests first: `docs/operations.md` section 2.7.

`docs/operations.md` section 2.7 is the runnable procedure and its checks.

### Why it downloads rather than converts

If a SIF is missing, the worker's job script falls back to converting it from Docker:

```bash
[ -f "$SIF" ] || apptainer pull "$SIF" docker://<ref>
```

That fallback now always fails.
The HPC's Apptainer is not setuid (`-rwxr-xr-x`, 1.5.2), so an unprivileged pull runs `mksquashfs` under `proot`, `proot` needs `ptrace`, and the compute nodes refuse it:

```text
proot error: ptrace(TRACEME): Operation not permitted
```

Login nodes are not available for this, so nowhere on the cluster can do the conversion.
It downloads every layer and then dies at the last step, every time.
Before that, the first flow to need a new SIF built a multi-GB image inside its own SLURM job, and the smallest jobs ran out of memory doing it.

So CI converts each image and publishes the SIF beside it as an ORAS artifact.
On the HPC that is a plain download: no squashfs, no proot, no ptrace, and no out-of-memory risk.

### By hand

Use this only for an environment `utils-build-environments` cannot fetch.
Replace `<storage.hpc_alias>` and `<registry.images>` with the values from `site.yaml`:

```bash
# HPC login node
export APPTAINER_CACHEDIR=<storage.hpc_alias>/../apptainer-cache
export APPTAINER_DOCKER_USERNAME="$GHCR_USERNAME"
export APPTAINER_DOCKER_PASSWORD="$COMX_GHCR_READ"

ENV_DIR=<storage.hpc_alias>/environments   # worker's sif_fallback_root

# the light image - check-qptiffs and the other lamindb-only stages
apptainer pull "$ENV_DIR/comx-qptiff_cx0.1.0-bio23e46aa.sif" \
  oras://<registry.images>/comx-qptiff-sif:cx0.1.0-bio23e46aa

# ALL FOUR croi SIFs - img-croi publishes these as ORAS artifacts, one per objective per mode.
apptainer pull "$ENV_DIR/croi-general-inference_v0.3.5.sif" \
  oras://<registry.images>/croi-general-inference-sif:v0.3.5
apptainer pull "$ENV_DIR/croi-core-inference_v0.3.5.sif" \
  oras://<registry.images>/croi-core-inference-sif:v0.3.5
apptainer pull "$ENV_DIR/croi-general-train_v0.3.5.sif" \
  oras://<registry.images>/croi-general-train-sif:v0.3.5
apptainer pull "$ENV_DIR/croi-core-train_v0.3.5.sif" \
  oras://<registry.images>/croi-core-train-sif:v0.3.5

# bgnorm still converts from docker://, which hits the proot failure above.
# Prefer keeping the existing file.
```

- The `oras://` name is the image name plus `-sif`, and the artifact tag equals the image tag exactly.
- If a pull returns 404, CI has not published that artifact yet. Check the build ran, rather than reaching for `docker://`.

### Two images for HPC stages

- 13 of the 19 `hpc-ssh-pool` deployments run the light `comx-qptiff` image.
- The 5 that need geopandas, shapely or matplotlib stay on croi: the detect and persist stages, plus `pipeline-data-tests`, which reaches geopandas through `checks/pipeline.py`.
- bgnorm-core keeps its own image.

`tests/test_hpc_image_fit.py` walks each light deployment's imports, so an import the light image cannot serve fails in the suite, not inside a SLURM job.
The light image is about a sixth of croi's size and rebuilds in minutes.
Its tag encodes the framework version AND the reader commit, because SIFs are cached by path.

### Check every SIF exists

Run on the HPC. A missing SIF is otherwise invisible until a job hits it:

```bash
# HPC login node
grep -oE "sif_artifact: environments/\S+" prefect.yaml | awk '{print $2}' | sort -u | while read k; do
  p="<storage.hpc_alias>/$k"
  [ -f "$p" ] && echo "ok      $k" || echo "MISSING $k"
done
```

- The path is `sif_fallback_root` plus the `sif_artifact` KEY, with `_` before the tag, not `:`. A wrong filename reads as absent and pulls again.
- A partial file from a killed pull PASSES the `-f` test, and `apptainer exec` then fails with an unrelated-looking error. Compare sizes against a known-good sibling (croi inference ~5.6GB, bgnorm ~0.5GB) and delete anything short.

Keep the previous tag's SIFs until the new ones are proven: they are the rollback.

## 5. Update the VM worker's venv

**`bootstrap.sh` will not do this.**
It installs only `if [ ! -x "$VENV/bin/prefect" ]`, so on an existing venv it logs "dedicated venv present, skipping build" and installs nothing.
The worker keeps the old `clinicalomx`, and the deploy looks done.

Do NOT delete the venv to force a rebuild.
It also holds prefect, lamindb, mlflow, streamlit, pandas and numpy; a rebuild downloads all of it again, and a failure part-way leaves a production worker with no venv.
Re-run the install instead.
It is the same command bootstrap uses, with the same one-shot credential injection, so no token lands on disk:

```bash
ssh ubuntu@<hub.public_address>                      # from site.yaml
cd ~/clinicalomx_pipeline && git pull                 # VM stages read the working tree
cd ~/clinicalomxdb/hub-pipelines/prefect/workers/vm-process
git -C ~/clinicalomxdb pull                           # picks up the bumped clinicalomx pin

sudo -u "$(stat -c '%U' .)" env \
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0="url.https://${GHCR_USERNAME}:${COMX_CI_READ}@github.com/.insteadOf" \
  GIT_CONFIG_VALUE_0="https://github.com/" \
  .venv/bin/pip install --upgrade -e .

.venv/bin/python -c "import clinicalomx; print(clinicalomx.__version__)"   # expect 0.2.4
sudo systemctl restart vm-pool-worker
```

- The run user is the OWNER of that directory (`stat -c '%U'`), not a fixed account.
- The credential variables are `GHCR_USERNAME` and `COMX_CI_READ` (the contents-read token), not `GITHUB_TOKEN`.
- Keep `--upgrade`: without it pip may keep the installed `clinicalomx` and miss that the direct URL's tag moved.

A full `rm -rf .venv && ./bootstrap.sh` is right only when the Python interpreter changes or the venv is actually broken.

## 6. Seed the vocabulary

The persist stages no longer create a missing feature as a side effect, so this must run before the first persist.

**`--with-consumers` sees only the consumers pip-INSTALLED where you run it**, because `discover()` reads installed entry points.

| Run it from | Sees | Use it for |
|---|---|---|
| croi SIF | foundation + croi | croi's features |
| VM worker venv | foundation only | the foundation; croi is not installed there |
| anywhere | - | this repo's own features - it is NOT installed anywhere, see below |

```bash
# HPC node
SIF=<storage.hpc_alias>/environments/croi-core-inference_v0.3.5.sif
apptainer exec "$SIF" python -m clinicalomx.vocab plan  --with-consumers   # look first
apptainer exec "$SIF" python -m clinicalomx.vocab apply --with-consumers
apptainer exec "$SIF" python -m clinicalomx.vocab check                    # hashes now agree
```

- **This repo's own vocabulary needs no step.** Its flows call `vocab.ensure()`, which builds the `Vocabulary` directly, so its features are created on first run. As a result `audit` anywhere reports them as `(unowned)`: a reporting gap, not a functional one.
- The ledger hash covers the FOUNDATION's declared state only (`vocab/data/` plus the step names), not consumer vocabularies. So stamping it from the SIF, which cannot see this repo, causes no churn.

Then check:

```bash
python -m clinicalomx.checks run          # expect 0 errors
```

## 7. Re-register deployments and automations

```bash
cd ~/clinicalomx_pipeline
prefect deploy --all
python -m qptiff_pipeline.orchestration.automations
```

`prefect deploy` creates and updates but **never deletes**.
A deployment removed from `prefect.yaml` stays on the server until you delete it by hand.

**Delete by the FULL `flow-name/deployment-name`.** A bare deployment name is not found.
Read the names from the server, not from the module names: some flows are named differently from their module.

```bash
prefect deployment ls          # the source of truth for these names
```

The eleven deployments left behind when nine flow modules were deleted:

```bash
for d in core-backfill-artifacts/utils-core-backfill \
         backfill-image-type/utils-backfill-image-type \
         backfill-image-type/pipeline-backfill-image-type \
         backfill-linked-entities/utils-backfill-linked-entities \
         backfill-linked-entities/pipeline-backfill-linked-entities \
         backfill-accepted-links/utils-backfill-accepted-links \
         backfill-roi-anchors/utils-backfill-roi-anchors \
         migrate-keys/utils-migrate-keys \
         consolidate-slide-folders/utils-consolidate-slide-folders \
         regenerate-slides/utils-regenerate-slides \
         reprocess-storage-migration/utils-reprocess-storage-migration; do
  prefect deployment delete "$d" || true
done
```

Leave alone anything this repo does not own: `hpc-health-check`, and croi's `core-train-and-eval/train-core` and `general-train-and-eval/train-general`.

## 8. Run one slide end to end

```bash
prefect deployment run general-detect/pipeline-general-detect -p slide_uids='["<uid>"]'
```

1. Accept it in the review app.
2. Let the automations carry it: general-persist, core-detect, accept, core-persist.
3. Re-run a detect flow on the same unit. Confirm `skip compute (exists)` still logs *while reconcile still runs*.

Step 3 proves the cached path of `materialize`, the state guard that must not rewind a gate a human moved past, and the idempotency guard in `annotate`: re-annotating must leave exactly one value, not two.

## Smaller cases

- **Flow code only:** push, then `git pull` on the VM. The HPC re-clones per run.
- **`prefect.yaml` only:** push, then `prefect deploy --all`. If the change touched a `sif_artifact` tag, it is NOT a smaller case: do step 4.
- **Graph topology only:** push, then `python -m qptiff_pipeline.orchestration.automations`. `tests/test_automations.py` must pass first: it pins every edge.
