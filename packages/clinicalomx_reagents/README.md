# clinicalomx_reagents

The lab's reagent inventory: conjugated antibodies, the reporter barcodes that reveal them, and the dyes those reporters carry.
The people who stock the reagents maintain it.

It reads and validates three CSV files.
It has no runtime dependency, holds no claims and opens no database.
`clinicalomx_markers` uses it.

## Install

Requires Python 3.11 or later.

```bash
cd packages/clinicalomx_reagents
uv sync
```

No private index credential is needed.

## Use

```python
from clinicalomx_reagents import inventory, rows

antibodies = rows.antibodies()        # data/antibodies.csv, validated against spec.py
barcodes = rows.barcodes()
fluorophores = rows.fluorophores()

inventory.reporter_for("BX001")       # the barcodes.csv row that pairs with an antibody's barcode
inventory.file_problems(antibodies, barcodes, fluorophores)   # every problem, as strings
```

The checks report problems instead of raising, so one pass names all of them.
The full import check is `clinicalomx_markers.domain.reagent_inventory.validate_conjugates`, which adds the two checks that need the marker registry.

## Layout

The code is in `src/clinicalomx_reagents/`.

| Module | Holds |
|---|---|
| `spec.py` | what each CSV may contain, one `SeedFile` per file |
| `rows.py` | reads `data/`, validated against the spec |
| `inventory.py` | the BX to RX pairing and the relational checks |
| `docs.py` | generates the tables in `data/README.md` |
| `data/` | `antibodies.csv`, `barcodes.csv`, `fluorophores.csv`. Their format is in `data/README.md` |

## Change the data

1. Edit the CSV in `src/clinicalomx_reagents/data/`.
2. If you changed a column, change `spec.py` too, then regenerate the tables:

   ```bash
   uv run python -m clinicalomx_reagents.docs
   ```

3. Run the tests.

## Test

```bash
uv run pytest -q
uv run ruff check src tests
```

CI runs both from `.github/workflows/pkg-clinicalomx-reagents.yml`.
