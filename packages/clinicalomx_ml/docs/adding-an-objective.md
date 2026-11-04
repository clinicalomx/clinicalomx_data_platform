# Adding an objective

How to add a new trainable task, called an objective, to `roi_detection`.
Copy one of the two worked examples, `tma_core` or `tissue_objects`, and follow the steps below.

Words used below:

- **Objective**: one prediction task with one class taxonomy and one line of champion models.
- **Member**: a whole problem domain with its own data and container base. `roi_detection` is the only member.
- **Wheel**: the installable Python package. One wheel, `clinical-roi`, carries every objective.
- **Image**: a Docker container build. On the HPC each image is converted to a **SIF**, the single-file format Apptainer runs, because the HPC has no Docker.
- **lamindb**: the platform's data catalogue. Every input and output is a versioned lamindb Artifact or Collection.
- **MLflow**: the experiment tracker and model registry. An **alias** such as `champion` points at one registered model version.
- **Prefect deployment**: a named job on the platform's Prefect server, run on the HPC or the hub VM.

## 1. Decide: objective or member

Add an objective when your task reads the same governed data as `roi_detection` and writes a different prediction over it.
`tma_core` is an objective even though it predicts different classes, because it reads `tissue_objects`' output and shares its images, container base and storage root.

Add a member only when nothing is shared.
A member needs a new container base, a new image build matrix, a new storage layout, and a recorded design decision before any code.
Assume objective until you can show a member would share nothing with `roi_detection`.

The rest of this page is about objectives.

## 2. Create the directory

An objective is ONE directory, holding everything about that objective and nothing shared:

```text
roi_detection/src/croi/objectives/<obj>/
    __init__.py       what the objective detects, and a map of its modules
    objective.py      its DetectionObjective record
    registry.py       its ChampionRegistry: registered-model name and alias namespace
    dataset.py        its DetectionDataset subclass, implementing pairs_in
    vocab.py          the Vocabulary this objective OWNS, and its ROI_SCALARS
    config.py         one model per stage yaml, over the shared ones in croi.config
    cli.py            SUBCOMMANDS: its own stages of `python -m croi <obj> <stage>`
    environment.py    one ObjectiveEnvironment bound to its configs/
    labels.py split.py and any other stage of its own
    flows/            its Prefect flows, one file per deployment
    configs/
        manifest.yaml the environment: instance, project, storage root, MLflow
        <stage>.yaml  one per stage, SHIPPED IN THE WHEEL as package data
```

`croi/io/objectives.py` states the template as a list of named parts.
Make `__init__.py` an ordinary package, never a PEP 420 namespace.

### The record

The record is four fields, in `croi/io/objectives.py`:

```python
@dataclass(frozen=True)
class DetectionObjective:
    name: str                       # its directory, e.g. "tma_core"
    lamin_key_prefix: str           # the word stored keys use, e.g. "clinical-roi/core"
    registry: ChampionRegistry
    dataset: type[DetectionDataset]
```

- `croi.train` and `croi.evaluation.evaluate` read `registry`, and load `dataset` through `load_dataset`.
- `vocabulary()` and the shared stages find the objective's modules by `name`.
- `roi_detection/tests/test_objectives.py` reads `lamin_key_prefix` and fails when a config key leaves it.

The record is data, not a base class: every stage is a free function that takes its config.

### The dataset

Subclass `croi.io.datasets.DetectionDataset` and implement `pairs_in`: how one split Collection's artifacts pair into (image, label), and which labels matched no image.
The base class already holds the views, counts, validation and the COCO dataset every trainer is handed (`to_coco`, a `croi.io.coco.CocoSplits`).

Do not write a YOLO tree or a COCO file.
Each trainer writes its own framework's view of the COCO dataset.

If your splits can leak through a unit coarser than the image, also override `leakage_findings`.
`tma_core` does this for the slide its cores were cut from.

### Stages

A stage that is a pipeline of its own (labels, split, dearraying, thumbnailing) stays a module in your directory.
Declare it as a `croi.config.Subcommand` in your `cli.py`, naming its default yaml and that yaml's model.

`train`, `validate` and `resolve-model` are shared by every objective (`croi.cli.SHARED_SUBCOMMANDS`).
Your objective may not shadow one.

Everything shared lives in `croi`: the data layer `croi.io`, the stage config models, the detector and trainer seams, train and evaluate, the shared CLI stages, the ROI schema and the stage environment.
Nothing in `croi` imports a particular objective.
It gets the record from `croi.objectives.get(name)`.

## 3. Register it

Do these four, in order:

1. Add the objective's name and record attribute to `_RECORDS` in `croi/objectives/__init__.py`.
   Its stages are then reachable as `python -m croi <obj> <stage>`.
2. Add its inference dependencies to `[project.dependencies]` in `packages/clinicalomx_ml/pyproject.toml`, and anything only training needs to the `train` extra.
3. Add the objective name to the objective loop in `roi_detection/containers/build.sh`.
   `base.Dockerfile` needs nothing.
4. Add a deployment per orchestrated stage to `roi_detection/prefect.yaml`, with a flow file in your `flows/`.
   Read [Release it](#8-release-it) before you assume a platform tag ships a change to it.

## 4. Declare the vocabulary

A vocabulary is the set of lamindb feature and type names your objective writes.

1. Declare every name in your `vocab.py` as a `clinicalomx.vocab.Vocabulary`.
2. Add one row for it to the entry-point table in `packages/clinicalomx_ml/pyproject.toml`:

   ```toml
   # one row per objective, and the OWNER must be distinct
   [project.entry-points."clinicalomx.vocabulary"]
   croi-<obj> = "croi.objectives.<obj>.vocab:VOCAB"
   ```

The owner must differ from every other objective's.
`clinicalomx.vocab.discover()` keys on `Vocabulary.owner`, so a shared owner silently lets one objective's declarations overwrite the other's.

Declare every name in that ONE module, even a name only training writes, so both sides can import it.

**Never write a bare feature string.**
`vocab.feature()` returns the name, so import the constant (`vocab.DETECTIONS`), never `"detections"`.
A bare string in `set_values` creates a permanent `Feature` with a guessed type, no description and no owner, and a used `Feature` cannot be deleted.

The wheel has one source root, `roi_detection/src/croi`, so your objective needs no packaging line of its own.
`roi_detection/tests/test_packaging.py` refuses a second root that shares a destination, because the editable install would silently lose one.

## 5. Write the configs

### The manifest

`configs/manifest.yaml` declares the environment once:

```yaml
instance: "${LAMIN_INSTANCE}"

project:
  project_name: "clinical-roi-<obj>"
  project_description: "<what this objective predicts>"

storage:
  storage_root: "${CLINICALOMX_SITE_MODEL_ROOT}"
  region: hpc

services:
  mlflow:
    uri_env: MLFLOW_TRACKING_URI
```

- `environment.load_manifest` validates it, and layers overrides in the order `init > env (CROI_*) > .env > yaml`.
- `cli.py` calls `environment.preflight()` once for every stage, so a missing instance or an unmounted storage root fails at once, not halfway through a GPU job.
- `configs/` sits inside your directory because `clinicalomx.stage_config_dir` resolves the `configs/` beside the calling file, `environment.py`. So the wheel carries the configs and the image needs no copy step.
- `$CROI_CONFIG_DIR` still overrides.

### One yaml per stage

Each `configs/<stage>.yaml` has the same four blocks:

- `data_dir`: the temporary staging folder under the storage root.
- `inputs`: the governed Artifacts and Collections it reads, each an `InputRef` (`{kind, uid|key|version, source}`). An input chosen by filter is a plain `{query: ...}` block the driver reads.
- `outputs`: key and description pairs, with a `key_template` on a per-item producer.
- `parameters`: the tunables.

That grammar is `croi.config.StageConfig`.

1. Subclass it in your `config.py` to name the keys each stage reads.
   The shared train stage's model is `croi.config.TrainConfig`, with one parameter model per framework chosen by `framework:`.
2. Add a `Subcommand` in `cli.py` naming that model.

Every model is strict and refuses an unknown key.
`environment.load_config(name, schema)` validates the file before preflight, so a typo such as `epochz: 200` fails at load, naming the file and the key.
A field keeps the optionality the stage gives its key, and never restates the stage's default.
`roi_detection/tests/test_configs.py` loads every packaged yaml through its stage's model.

**`data_dir` must equal the key prefix its outputs are written under.**
An artifact's key IS its path under the storage root.
If the two differ, the files land outside their key and the lineage link points at nothing.

### Storage keys

The key layout is fixed:

```text
objective-private substrate       <role>/                  e.g. thumbnails/
objective-private annotations     <objective>_labels/      distinct taxonomies never mix
cross-objective data              shared/<dataset>/        the contract between objectives
models                            models/<objective>/      one champion line per objective
```

Choose a key once.
A key is part of an artifact's identity, so renaming one means registering it again, and moving the files leaves every old artifact pointing at nothing.

### Compute and reconcile

`clinicalomx.materialize([keys], compute, reconcile)` skips `compute` when the outputs already exist, but **always** runs `reconcile`.
Put anything idempotent and metadata-shaped in `reconcile`: the ROI wiring, the lineage links and the feature annotation.
In `compute`, a re-run that skips work would silently stop maintaining them.

### Lineage

Always record a parent after the save, on the saved artifact's own run:

```python
<saved_artifact>.run.input_artifacts.add(parent)
```

`ln.context.run.input_artifacts.add(...)` inside an `@ln.step` writes to a different run object, so the link silently never lands.

## 6. Keep inference free of the training stack

An inference install must never import MLflow or a training module.
Training and inference are one wheel, and the `train` extra is the only difference.

- Put a dependency only training needs in the `train` extra, never in `[project.dependencies]`.
- Add your objective to `roi_detection/tests/test_inference_has_no_train_stack.py`:
  - its `registry` in `ENTRY_POINTS`;
  - its own training module set, beside `TISSUE_TRAIN_MODULES`, `CORE_TRAIN_MODULES` and `CROI_TRAIN_MODULES`. Today an objective has no training module of its own, so its set is empty.

The test walks the imports reachable from each objective's inference entry points and refuses an MLflow or training import anywhere in them.
A module no set names counts as inference-side, which is the safe direction.
A test asserts the objectives' sets mirror each other.
`KNOWN_MLFLOW_IN_INFERENCE`, the named exemption list, is empty, and `test_a_planted_offender_is_caught` proves an empty list still catches an offender.

MLflow is still installed in every image, because the base dependency `clinicalomx[models]` brings it.
The rule is about this package's own imports, not about what is installed.

### Why images split by mode

Only inference images are converted to SIFs and published by default (`SIF_MODES` in `containers/build.sh`).
Converting a Docker image to a SIF on the HPC runs `mksquashfs`, which sizes its buffers from the node's RAM rather than the job's memory limit.
A large enough image therefore runs out of memory inside its own SLURM job, and asking for more memory does not help.

A train image needs a SIF only when its deployment's flow code comes from the wheel rather than from the cloned repository.
`SIF_MODES` takes an `<obj>/<mode>` entry, so one train image can be published without converting all of them.

## 7. Hand a trained model to its consumers

The handoff is a versioned lamindb Artifact at a stable key, and nothing else.
A consumer never reads a repository path or a checkpoint file.

1. The train stage writes `models/<objective>/best.pt`.
   lamindb cuts a new version each time, so the champion line is one key with a version history.
2. The same checkpoint is logged to MLflow and registered under your registered-model name.
   Declare that name as `REGISTERED_MODEL` and `ALIAS_NAMESPACE` in your `registry.py`.
3. A consumer loads it through your registry module:

   ```python
   from croi.objectives.tissue_objects.registry import load_champion, resolve_model_artifact
   ```

`load_champion` returns a framework-agnostic `ObjectDetector`, built with whichever framework the champion records (`registry.framework_of`).
The raw framework object is its `.model`.
Production uses this, in `clinicalomx_pipelines`' `flows/general_detect.py` and `flows/core_detect.py`.

`clinicalomx.ModelRef` behind it uses the local lamindb weights when the storage root is mounted, and downloads the MLflow copy when it is not.
That is what lets the same code run on the HPC and on the hub VM.

A new framework is a package under `croi/detection/backends/` holding its detector and trainer, an entry in `DETECTORS` and `TRAINERS`, its name in `croi.config.Framework` with a parameter model, and a stage config naming it.
`docs/two-backends.md` shows the two that exist.

**An objective-to-objective handoff is a release, not a live input.**
`tissue_objects` writes TMA crops, and `tma_core` trains on them.
`tma_core` pins `{key, version}` explicitly, and the producing stage stamps the crop Collection's `release_version` with the model version that cut it.
So a better `tissue_objects` model never silently changes `tma_core`'s training data.
Build any new cross-objective dependency the same way.

**`promote()` has no gate.**
It checks no metric, compares no challenger against the champion, and records no approval.
It only moves the alias, so an alias does not mean anything was checked.

## 8. Release it

Two pipelines release different things, both triggered by a `clinical-roi-v*` git tag.

**The wheel.** `.github/workflows/release-clinical-roi.yml` publishes `clinical-roi` to the lab's private Python package index.

- The tag's version must equal `version` in `packages/clinicalomx_ml/pyproject.toml`. The workflow refuses a mismatch.
- Resolving and publishing need the index read credential (`INDEX_READ_ACCESS_KEY_ID` / `INDEX_READ_SECRET_ACCESS_KEY`), because the bucket is private.

**The images.** `.github/workflows/img-croi.yml` runs `roi_detection/containers/build.sh`.
It builds `croi-base` plus one image per objective and mode, converts the inference images to SIFs, and publishes each SIF as an ORAS artifact (a file stored in the container registry).
Each image is tagged with the tag's version.

- The wheel inside each image is built from the checked-out code, so a new objective builds before anything of it is published.
- Every image holds every objective's code. `CROI_OBJECTIVE` is what makes an image one objective's image.

Three rules that each cause a change to silently not ship:

1. **A flow's code comes from the IMAGE, not from the platform tag.**
   Prefect puts only the flow file's own folder and the working directory on `sys.path`, never `roi_detection/src`, so `croi.*` resolves to the installed wheel.
   A change under `src/croi/` needs a new `clinical-roi-v*` tag and an image adoption: `docs/operations.md` section 1.2.
2. **Bump `prefect.yaml` in the commit you tag.**
   Move `image:` and `sif_artifact:` together, for every deployment.
3. **The HPC caches a SIF by its path.**
   `sif_artifact: environments/<owner>/<name>_<tag>.sif` is fetched again only when the tag changes.
   New contents under an old tag are never picked up, so a dependency bump also needs a new tag.

## 9. Pins you must not loosen

The package's shared dependencies:

```toml
dependencies = [
    "clinicalomx[models]>=0.2,<0.4",  # version RANGE, never a git pin; see the note below
    "lamindb==2.8.1",                 # EXACT, and must match every environment on the instance
    "prefect>=3.6,<4",                # the HPC worker runs `prefect flow-run execute` in the SIF
    # then each objective's runtime: the model library, the readers, the array stack
]
```

- **`clinicalomx` is a version range from the private index, never a git pin.**
  uv refuses two different git sources for one package, so a git pin here constrains every package downstream.
  To develop against a local copy, run `uv pip install -e ../clinicalomx`.
- **`lamindb==2.8.1` is exact.**
  A lamindb behind the instance's database schema refuses to connect.
  Mixed versions also once left two spellings of one `Artifact.suffix` on the instance, which broke every filter on it.
  Bump the instance and every pin together.
- **There is ONE version field.**
  Keep it that way: `uv version --project <dir>` walks up to a parent `pyproject.toml` when `<dir>` has none, so a check across several version fields can pass while checking nothing.

## Naming

The distribution is `clinical-roi`.
`croi-` is the image prefix and the vocabulary owner prefix.
Do not rename either: the private index, cached SIF paths on the HPC, Prefect deployments on the server and `Feature` owners in the live instance all refer to them.
A new objective inside `roi_detection` uses the `croi-` prefix.
