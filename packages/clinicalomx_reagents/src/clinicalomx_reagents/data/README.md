# `data/`

The reagent inventory's three hand-edited CSVs: conjugated antibodies, the reporter barcodes that reveal them, and the dyes those reporters carry.

The tables below are generated from `../spec.py`.
Regenerate them after changing the spec:

```bash
uv run python -m clinicalomx_reagents.docs
```

`tests/test_spec.py` fails if the tables and the spec disagree.

<!-- BEGIN GENERATED from spec.py: python -m clinicalomx_reagents.docs -->

| File | Rows | Holds |
|---|---|---|
| `antibodies.csv` | 4 | The reagent: catalogue number, vendor, target label, marker, isoform, BX barcode |
| `barcodes.csv` | 4 | Reporter product identity: RX barcode, akoya id, fluorophore |
| `fluorophores.csv` | 4 | Canonical dyes: excitation and emission peaks, FluoroFinder url |

Every column below is validated when the file is read.
A row that breaks one of these rules raises rather than resolving to a default.

| File | Column | Holds | Rule |
|---|---|---|---|
| `antibodies.csv` | `catalogue_number` | The vendor's part number. One row per number | required, row label |
| `antibodies.csv` | `vendor` | Who sells it | optional |
| `antibodies.csv` | `target_label` | The vendor's spelling of the target, kept verbatim | optional |
| `antibodies.csv` | `canonical_name` | The marker it binds, in this lab's spelling | required |
| `antibodies.csv` | `uniprot_isoform` | The exact IsoId the clone binds. Blank means not isoform specific | a UniProt IsoId such as P08575-8, or blank |
| `antibodies.csv` | `bx_barcode` | The oligo it carries. Pairs with the RX of the same number | required |
| `antibodies.csv` | `akoya_id` | Akoya's product id for the antibody and reporter as a set | required |
| `antibodies.csv` | `status` | active or draft. Draft rows are validated on every run but never seeded | required, one of `active`, `draft` |
| `barcodes.csv` | `rx_barcode` | The reporter oligo. Reveals the BX of the same number | required, row label |
| `barcodes.csv` | `akoya_id` | Akoya's product id for the antibody and reporter as a set | required |
| `barcodes.csv` | `fluorophore` | The dye it carries, by canonical name | required |
| `barcodes.csv` | `status` | active or draft. Draft rows are validated on every run but never seeded | required, one of `active`, `draft` |
| `fluorophores.csv` | `canonical_name` | The dye, named once per lab | required, row label |
| `fluorophores.csv` | `excitation_nm` | The vendor's nominal excitation peak, in nm | optional |
| `fluorophores.csv` | `emission_nm` | The vendor's nominal emission peak, in nm | optional |
| `fluorophores.csv` | `fluorofinder_url` | Spectra. Stays empty until someone has opened it | optional |
| `fluorophores.csv` | `aliases` | Other spellings of the dye, pipe separated | optional |
| `fluorophores.csv` | `status` | active or draft. Draft rows are validated on every run but never seeded | required, one of `active`, `draft` |

<!-- END GENERATED -->

## Format

- Lines starting with `#` are comments and are stripped on read.
- Every row is an example and `status=draft`. `test_the_whole_inventory_is_still_draft` fails the day a row becomes `active`.
- Files link by canonical name: `barcodes.csv` to `fluorophores.csv`, and `antibodies.csv` to the marker registry's `markers.csv`.
- The BX to RX pairing is not stored. An antibody's BX barcode pairs with the reporter whose RX barcode has the same number.
- `akoya_id` names the paired set, so both halves carry the same id. `NA` means a custom conjugation with no bundled reporter. An empty `akoya_id` is rejected.
- `fluorofinder_url` is empty on every row.

`../inventory.py` runs the relational checks.
`clinicalomx_markers.domain.reagent_inventory.conjugate_problems` adds the two checks that need the marker registry.
