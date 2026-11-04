# clinicalomx

The Python library for reading and writing the clinicalOMx lamindb instance (`lamin.instance` in `site.yaml`) with shared names.

lamindb is the database and file registry that holds every slide, image and result.
In lamindb, an **Artifact** is a registered file, a **Record** is a registered row, and a **feature** is a named value on either.

## Terms

- **ROI**: a region of interest. An `ln.Record` with a subtype: `WholeSlide`, `TMA`, `Core` or `Tile`. Names form a tree: `<slide>`, `<slide>:TMA0`, `<slide>:TMA0:R2C4`.
- **Role**: what an artifact is to its ROI, named by a feature such as `source_image`, `bgnormed` or `segmentations`.
- **Vocabulary**: the feature and type names an instance must contain, kept as data files.
- **Gate**: a point where a pipeline stops for a human, stored as `pipeline_state` on a Record.
- **Recipe**: a product an ROI can be turned into, such as `ometiff.core` or `spatialdata.tma.by-core`.

## Install

No version is published to a public index.
Add a path source to your `pyproject.toml`:

```toml
[tool.uv.sources]
clinicalomx = { path = "../clinicalomx" }
```

| Extra | Adds | Needed for |
|---|---|---|
| `vocab` | bionty | the vocabulary seeder |
| `extract` | the image and table format libraries | building an export |
| `app` | reflex | the Extract Explorer UI |
| `prefect` | prefect | the Prefect event emitter for gates |
| `models` | mlflow | the MLflow fallback in `ModelRef` |

## Use

```python
import clinicalomx as cx
from clinicalomx.features import upsert_values

cx.FEATURES.ROI_UID                                  # 'roi_uid', no database needed
cx.update(input_art, output_art, "segmentations")    # 1:1, the output inherits the input's ROI
cx.split(tma_art, [(core_roi, core_art)], "image")   # 1:N
roi = cx.require_roi(artifact)                       # raises if the artifact has no ROI
upsert_values(record.features, {cx.FEATURES.PATIENT_ID: "P0001"})
```

Write feature values with `upsert_values`, never `record.features.set_values`, which replaces every other value on the record.

Get data out with the extract CLI, in this order.
`docs/extract.md` walks through each step.

```bash
clinicalomx-extract find      "core R2C4 in TMA0 of the Arutha slide"
clinicalomx-extract describe  "<slide>:TMA0"
clinicalomx-extract plan      --roi "<slide>:TMA0:R2C4" --target OME-TIFF --dest ./out --out plan.json
clinicalomx-extract assemble  --plan plan.json --yes
./run_extract_app.sh          # the Explorer UI on demo data, http://localhost:8601
clinicalomx-checks run        # read-only invariants; non-zero exit on an error
```

| To | Read |
|---|---|
| get data out of the instance | [docs/extract.md](docs/extract.md) |
| anchor artifacts to ROIs | [docs/roi-layer.md](docs/roi-layer.md) |
| find a name to import | [docs/reference.md](docs/reference.md) (generated) |
| see every recipe | [docs/extract-recipes.md](docs/extract-recipes.md) (generated) |
| declare your own features, checks, policies or recipes | [docs/extending.md](docs/extending.md) |
| seed or audit the vocabulary | [docs/vocabulary.md](docs/vocabulary.md) |
| use `Manifest`, `InputRef` or gates | [docs/data-layer.md](docs/data-layer.md) |
| move off `dataspec` or `roispec` | [docs/upgrading-to-v0.3.0.md](docs/upgrading-to-v0.3.0.md) |

## Test

From `packages/clinicalomx`:

```bash
uv sync --extra vocab --extra extract --extra app
uv run pytest -q
uv run ruff check src tests scripts --no-cache
uv run python -m clinicalomx.vocab docs --check docs/reference.md
uv run python -m clinicalomx.extract docs --check docs/extract-recipes.md
```

Sync all three extras, or the extract tests skip.
If `pytest` exits 139 with no output, use a uv-managed interpreter: `--python-preference only-managed --python 3.12`.

Two files need a live instance and skip otherwise.
Point them at a throwaway instance, never the hub:

```bash
CLINICALOMXDB_SCHEMA_TEST_INSTANCE=1 uv run pytest tests/test_vocab_dtypes.py tests/test_vocab_idempotent.py
```

## Change

- `docs/reference.md` and `docs/extract-recipes.md` are generated. Regenerate them with `python -m clinicalomx.vocab docs > docs/reference.md` and `python -m clinicalomx.extract docs > docs/extract-recipes.md`.
- A foundation feature is a row in `src/clinicalomx/vocab/data/features.csv` and a constant in `names.py`.
- A public name is a row in `_EXPORTS` in `__init__.py`. Never remove a row.
- `lamindb==2.8.1` is pinned exactly. Bump it here, in `clinicalomx_pipelines`, the croi wheels, the HPC workers and the hub images together.
- To release, follow `docs/operations.md` section 1.1 at the repository root.
