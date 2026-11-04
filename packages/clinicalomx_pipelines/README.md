# clinicalomx-pipeline

The Prefect pipeline that turns raw `.qptiff` slides into reviewed, background-normalised cores.
It orchestrates three libraries and holds no science of its own:

| Library | Does | Source |
|---|---|---|
| `clinicalomx` | lamindb vocabulary, ROI links, gate state | `../clinicalomx` |
| `clinical-roi` | TMA and core detection | `../clinicalomx_ml` |
| `bgnorm` | background normalisation | a pinned git tag, see `pyproject.toml` |

Prefect is the workflow scheduler.
A **flow** is one Python function it runs, a **deployment** is a flow registered on the server, and a **work pool** is where it runs: `hpc-ssh-pool` submits SLURM jobs to the HPC, and `vm-pool` runs on the hub VM.
HPC stages run inside Apptainer image files (SIFs).

## Install

```bash
uv sync                       # the control-plane install: flows, gates, review logic
uv sync --extra prefect       # add the Prefect orchestration layer
uv sync --extra compute       # add clinical-roi and bgnorm, as the HPC stages use them
```

`bgnorm` needs Python 3.12.

## Run the apps on fake data

From this directory:

```bash
qptiff_pipeline/dev_review_app.sh      # Review app (Streamlit), http://localhost:8599
qptiff_pipeline/dev_build_app.sh       # Build app (Reflex), http://localhost:8601
```

Neither touches lamindb, Prefect, the HPC or a credential.
Each reloads on save.
Stop it with Ctrl-C, and set `PORT=` to move it.
The Review app's fake data is in `qptiff_pipeline/src/qptiff_pipeline/review/dev/fixtures.py`.
The Build app is the `clinicalomx` Extract Explorer, so its UI code is in `../clinicalomx/src/clinicalomx/extract/app/web/`.

## How a slide moves through the pipeline

1. `register-qptiffs` registers new scans, and `check-qptiffs` checks each file can be read.
2. `mint-slides` creates a `WholeSlide` record per scan.
3. `general-detect` finds TMAs and tissue sections. **Stop 1**: a reviewer accepts the boxes.
4. `general-persist` creates the TMA records, and `core-detect` splits each TMA into cores. **Stop 2**: a reviewer accepts the core grid.
5. `core-persist` creates the Core records. **Stop 3**: a reviewer triages each TMA as multiplex or H&E.
6. `bgnorm-runner` runs `bgnorm-core` on each core of a multiplex TMA. **Stop 4**: a reviewer accepts the result.

Each stop is a `pipeline_state` value on the record, so restarting Prefect loses nothing.
The code is under `qptiff_pipeline/src/qptiff_pipeline/`.
`domain/states.py` declares every state and stage.
`qptiff_pipeline/prefect.yaml` declares the deployments, and `orchestration/automations.py` declares which event or finished flow starts each one.

Register the automations on the hub VM:

```bash
python -m qptiff_pipeline.orchestration.automations
```

Accept a gate by hand:

```python
from qptiff_pipeline.domain.states import Decision
from qptiff_pipeline.orchestration import gate

gate.decide("general", slide.uid, Decision.ACCEPT)
```

## Deploy

Follow `qptiff_pipeline/deployment.md`, and `qptiff_pipeline/containers/deployment.md` for a new image.
A rebuilt image takes effect only under a new tag: the HPC caches each SIF by its path.

## Test

```bash
uv sync --extra bgnorm --extra review
uv run pytest -q -rs --ignore=qptiff_pipeline/tests/test_review_dev_harness.py \
  --ignore=qptiff_pipeline/tests/test_review_triage_states.py \
  --ignore=qptiff_pipeline/tests/test_roi_anchoring.py \
  --ignore=qptiff_pipeline/tests/test_build_app.py
uv run pytest -q -rs qptiff_pipeline/tests/test_review_dev_harness.py \
  qptiff_pipeline/tests/test_review_triage_states.py \
  qptiff_pipeline/tests/test_roi_anchoring.py qptiff_pipeline/tests/test_build_app.py
uv run ruff check qptiff_pipeline/src qptiff_pipeline/tests qptiff_pipeline/tests_pg --no-cache
```

The review-app tests install a fake Prefect, so they run in their own session.
`.github/workflows/pkg-clinicalomx-pipeline.yml` also runs the real-Prefect tests and `qptiff_pipeline/tests_pg`, which needs Docker.

## Known gaps

- The `basis` values in `domain/governance.py` are placeholders for the real approvals.
- The `he_pending` lane has no downstream consumer.
- `orchestration/gate.py` has little test coverage.
- The live checks in `qptiff_pipeline/docs/CLINICALOMX_V0_2_MIGRATION.md` have not all been run.
