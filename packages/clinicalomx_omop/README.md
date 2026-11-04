# clinicalomx_omop

Patient metadata for the clinicalomx instance, stored in the OMOP Common Data Model.

OMOP is a standard table layout for clinical data (persons, visits, conditions, measurements), and SNOMED CT is the standard clinical vocabulary its codes come from.

Every OMOP table this platform uses is an `ln.Record` type, generated from the published OHDSI specification.

## Depends on

- `clinicalomx`, for the instance vocabulary.
- `clinicalomx_ledger`, for the claims that link an ROI to a patient.
- `pandas`.
- An ATHENA vocabulary download of your own, for the vocabulary tables.

## Install

Requires Python 3.11 or later.

```bash
cd packages/clinicalomx_omop
uv sync
```

## Get the vocabulary

This repository never contains SNOMED CT or any other OMOP vocabulary table.

1. Download your own from [ATHENA](https://athena.ohdsi.org), the OHDSI vocabulary download site, under your own SNOMED CT licence.
2. Unzip it.
3. Point `CLINICALOMX_ATHENA_DIR` at the unzipped directory.

If it is missing, `clinicalomx_omop.athena.open_athena()` says what to get.

## Link an ROI to its patient

```python
from clinicalomx_omop import declare, link

declare.ensure()                                            # the record types, once
link.add_row(CURRENT, "person", {...})                      # a person row
link.propose(store, roi=core.name, person_id=1, ...)       # a ledger claim, cited
store.decide(claim, outcome="accepted", judgement=...)      # a human accepts it
link.project(store, core)                                   # core.patient -> that person
link.training_frame(cores, x=[...], y=[("person", "year_of_birth")])
```

The link from an ROI to a patient is a claim in the `patient` ledger domain.
The ROI's `patient` value is rebuilt from accepted claims, never written directly.

## What a `person` is

Read [`src/clinicalomx_omop/cdm/v5_4.py`](src/clinicalomx_omop/cdm/v5_4.py).
It is generated from the OHDSI field-level specification and committed.

The nine modelled tables are `person`, `visit_occurrence`, `condition_occurrence`, `procedure_occurrence`, `drug_exposure`, `measurement`, `observation`, `death` and `specimen`.

| On the instance | Named | Holds |
|---|---|---|
| `ln.Feature` type | `OMOP_CDM_v5.4.<table>` | one `ln.Feature` per CDM field, with the spec's own description |
| `ln.Schema` | `OMOP_CDM_v5.4.<table>`, under Schema type `OMOP_CDM_v5.4` | those features; `minimal_set=True` with non-required fields optional, `maximal_set=True` so a field the CDM does not define is refused |
| `ln.Record` type | `OMOP_CDM_v5.4.<table>`, under `OMOP_CDM_v5.4` | the rows, validated by that schema on save |

`person` is the exception: its record type sits under the foundation's `Patient`, so a person row is what the `patient` feature on an ROI points at.

The CDM version is in every name.
A new CDM release is a second set of types beside the first.

## Regenerate after a CDM release

```bash
uv run python scripts/generate_cdm.py           # write cdm/v5_4.py from spec/
uv run python scripts/generate_cdm.py --check   # CI: fail if the committed module is stale
```

`spec/` holds the OHDSI CSVs unmodified (Apache-2.0), and `spec/source.toml` records their tag, commit and sha256.
A CDM bump is new files, new digests and a regenerated module, in one diff.

## Test

```bash
uv sync --group pg
uv run pytest -q                  # generator and vocabulary loader, no database
uv run pytest tests_pg -q         # a throwaway postgres:18 in Docker, synthetic data only
uv run ruff check src tests tests_pg scripts --no-cache
```
