# clinicalomx_ml

The machine-learning models integrated with the core data platform. This serves as a submodule repo which details how productionised ML models should be templated. 

Currently, it holds one live ML application in prodction: ROI detection, which finds the tissue sections and TMA cores on a slide using either the Ultralytics YOLO or Roboflow RF-DETR detection backends.


## Depends on

- `clinicalomx[models]` and `lamindb==2.8.1`. `clinicalomx` comes from the lab's private package index.
- Ultralytics and RF-DETR for detection, `bioio-qptiff` to read scans, and Prefect.

## Install

From this directory:

```bash
uv sync                      # inference, plus the dev group
uv sync --extra train        # add MLflow and the training stack
uv sync --extra notebooks    # add Jupyter, matplotlib and spatialdata-plot
```

Resolving needs the private index read credential.
`docs/researcher-setup.md` at the repository root says how to set it.
A virtual environment that already matches `uv.lock` resolves offline without it.

## Layout

```text
pyproject.toml           the one distribution, clinical-roi
docs/                    adding-an-objective.md, two-backends.md, tissue_seg.md
roi_detection/           the ROI detection member
  src/croi/objectives/   one folder per objective
  containers/            the image build. See containers/README.md
  prefect.yaml           the training deployments
  deployment.md          how to deploy it
  tests/  tests_pg/      the unit suite and the Postgres suite
```

An **objective** is one prediction task with its own classes and its own line of champion models:

- `general` (`croi/objectives/tissue_objects/`): tissue sections and whole TMA grids.
- `core` (`croi/objectives/tma_core/`): one TMA grid split into its cores.
- `tissue_seg`: planned, no code. See `docs/tissue_seg.md`.

To add a task, read `docs/adding-an-objective.md` first.
Most new tasks are a new objective inside `roi_detection`.

## Use

Every objective ships in one distribution, `clinical-roi`.

| Install | Holds | Used by |
|---|---|---|
| `clinical-roi` | retrieve the champion, run it, write the result | anything that consumes predictions |
| `clinical-roi[train]` | adds MLflow and the RF-DETR training stack | training jobs only |

Each container image runs a stage as `python -m croi <objective> <stage>`.
An inference install must not import the training stack; `roi_detection/tests/test_inference_has_no_train_stack.py` checks it.

Do not rename the distributions, the release tag pattern, the GHCR image names, the two Prefect deployment names or the vocabulary owner strings.
Systems outside this repository refer to each of them.

## Test

```bash
uv lock --check --offline
uv run --no-project --python 3.11 --with pytest --with numpy --with pyyaml --with pydantic \
  pytest roi_detection/tests -q
uv run --no-project --with ruff ruff check --no-cache roi_detection/tests
```

These are the commands `.github/workflows/pkg-clinicalomx-ml.yml` runs.
To release a new version, follow `roi_detection/containers/deployment.md`.
