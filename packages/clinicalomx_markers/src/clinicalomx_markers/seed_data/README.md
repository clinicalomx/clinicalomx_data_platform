# `seed_data/`

Five hand-edited CSVs that seed the marker registry.
Once seeded, the instance holds the registry.
The reagent inventory's CSVs are in `packages/clinicalomx_reagents/src/clinicalomx_reagents/data/`.

The tables below are generated from `../seed_spec.py`.
Regenerate them after changing the spec:

```bash
uv run python -m clinicalomx_markers.seed_docs
```

`tests/test_seed_spec.py` fails if the tables and the spec disagree.

<!-- BEGIN GENERATED from seed_spec.py: python -m clinicalomx_markers.seed_docs -->

| File | Rows | Holds |
|---|---|---|
| `markers.csv` | 9 | Canonical analytes: name, full name, status |
| `targets.csv` | 7 | Seed target claims: the candidate proteins a marker binds, and their kind |
| `channels.csv` | 9 | (marker, platform) to imaging role, plus compartment evidence |
| `structures.csv` | 17 | Curated UniProt localisation prose to the GO cellular component it names |
| `rules.csv` | 1 | Constraints promoted from rejections, enforced by domain.rules |

Every column below is validated when the file is read.
A row that breaks one of these rules raises rather than resolving to a default.

| File | Column | Holds | Rule |
|---|---|---|---|
| `markers.csv` | `canonical_name` | The lab's canonical analyte name | required, row label |
| `markers.csv` | `full_name` | The long name, so a reviewer reading a diff knows what this is | optional |
| `markers.csv` | `status` | active or draft. Draft rows are validated on every run but never seeded | required, one of `active`, `draft` |
| `targets.csv` | `canonical_name` | The marker the claim is about | required, row label |
| `targets.csv` | `kind` | How the candidates relate, which decides how localisations combine | required, one of `single`, `isoform_ambiguous`, `cross_reactive`, `complex_member` |
| `targets.csv` | `candidates` | UniProt accessions or IsoIds, pipe separated. Every one is kept | required, UniProt accessions or IsoIds, pipe separated |
| `targets.csv` | `evidence` | What the claim quotes, so a citation has text to show | required |
| `targets.csv` | `curated_by` | Who stated it. The claim's source_id | required |
| `channels.csv` | `canonical_name` | The marker this channel measures | required, row label |
| `channels.csv` | `platform` | The instrument the channel was acquired on | required |
| `channels.csv` | `segmentation_role` | What the channel is good for in segmentation | required, one of `nucleus`, `cyto_structural`, `marker`, `exclude` |
| `channels.csv` | `compartment` | Localisation terms, pipe separated. rules.csv matches on it | optional |
| `channels.csv` | `go_cc` | GO cellular component id, where one was recorded | optional |
| `channels.csv` | `uniprot_cc_raw` | The verbatim UniProt subcellular location text | optional |
| `channels.csv` | `uniprot_release` | Which UniProt release the evidence was read from | optional |
| `channels.csv` | `role_source` | Who or what decided the role | optional |
| `channels.csv` | `status` | active or draft. Draft rows are validated on every run but never seeded | required, one of `active`, `draft` |
| `structures.csv` | `uniprot_location` | The UniProt subcellular location term, verbatim | required, row label |
| `structures.csv` | `go_id` | The GO cellular component the term names | required, a GO identifier such as GO:0005737 |
| `structures.csv` | `go_name` | The GO term's name at that release, for a reviewer to read | required |
| `structures.csv` | `go_release` | The GO release the row was checked against | required, one of `2025-10-10` |
| `structures.csv` | `curated_by` | Who made the mapping | required |
| `rules.csv` | `predicate` | Which claim predicate the rule constrains | required, row label |
| `rules.csv` | `when_field` | A field on the subject to test | optional |
| `rules.csv` | `when_contains` | Substring match against that field, case insensitive | optional |
| `rules.csv` | `forbid_value` | The value that must not be proposed | optional |
| `rules.csv` | `reason` | Why, in the words of whoever rejected it | optional |
| `rules.csv` | `added_by` | Who promoted the rejection | optional |

<!-- END GENERATED -->

## Format

- Lines starting with `#` are comments and are stripped on read.
- Every row is `status=draft`. `test_nothing_is_active_yet` fails the day a row becomes `active`.
- An alias is a claim, not a seed row. Propose it through `propose/alias.py` or `propose/human.py`.
- A dye's other spellings go in the `aliases` cell of `fluorophores.csv`, in `clinicalomx_reagents`.
- `rules.csv` matches against the `compartment` column of `channels.csv`. Keep that column.
- Each `targets.csv` row becomes one target claim, attributed to its `curated_by`. Every candidate is kept, and `kind` decides how their localisations combine.
- A marker with no `targets.csv` row, such as DAPI, targets no protein.

## Reagent inventory checks

`domain/reagent_inventory.py` checks the reagent inventory against this registry:

1. An antibody's `canonical_name` must be a marker in `markers.csv`.
2. Its `uniprot_isoform` must be an isoform of a protein that marker's target claims name. Leave it blank when the clone binds every isoform.

`domain.reagent_inventory.validate_conjugates` is the check a real inventory import must pass.

## Known gaps

- Canonical names mix the lab's spelling (`NAKATPASE`) and UniProt's (`CD8A`).
- `markers.csv` holds 9 markers. The lab's panels use about 210.
