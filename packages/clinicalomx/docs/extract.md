# Getting data out

Four commands that turn "core R2C4 of this slide" into a file you can open.
Run them in order:

```bash
clinicalomx-extract find      "core R2C4 in TMA0 of the Arutha slide"
clinicalomx-extract describe  "<slide>:TMA0"
clinicalomx-extract plan      --roi "<slide>:TMA0:R2C4" --target OME-TIFF --dest ./out
clinicalomx-extract assemble  --plan plan.json --yes
```

## Install

Reading the catalogue and planning need only the framework.
Building needs the format libraries, which is the `extract` extra:

```bash
uv add "clinicalomx[extract]"        # the format libraries; pyproject.toml lists them
```

The package comes from a private index; the [README's install section](../README.md#install) has the full setup.
Check it works with the catalogue, which needs no instance and no extra:

```bash
clinicalomx-extract recipes          # the whole catalogue; no database, no format libraries
```

`plan` runs on a laptop against a metadata-only connection.

## 1. Find the ROI you mean

```console
$ clinicalomx-extract find "core R2C4 in TMA0 of the Arutha slide"

query   'core R2C4 in TMA0 of the Arutha slide'
parsed  {'tma': 'TMA0', 'cell': 'R2C4', 'slide_tokens': 'arutha'}

2 candidate(s), 2 tied for best:
  1.00  Arutha YTMA 471-4_Scan1-16-bit:TMA0:R2C4   [Core]  uid 8bQx...
        cell R2C4 exact; TMA0 exact; 1/1 slide token(s) matched
  1.00  Arutha YTMA 471-9_Scan2-16-bit:TMA0:R2C4   [Core]  uid 9cRy...
        cell R2C4 exact; TMA0 exact; 1/1 slide token(s) matched

  AMBIGUOUS: 2 ROIs match equally well. Ask which one was meant; do not choose.
```

When two ROIs tie, `find` stops.
Add more of the slide name until one matches exactly.
`plan` also refuses an ambiguous name.

ROI names follow this pattern:

```text
WholeSlide   <slide stem>                 Arutha YTMA 471-4_Scan1-16-bit
TMA          <slide>:TMA<t>               <slide>:TMA0
Core         <slide>:TMA<t>:R<r>C<c>      <slide>:TMA0:R2C4
```

If you already know the full name, skip this step.

## 2. See what it can give you

```console
$ clinicalomx-extract describe "Arutha YTMA 471-4_Scan1-16-bit:TMA0"

TMA  Arutha YTMA 471-4_Scan1-16-bit:TMA0   uid 4kZq...
parent: Arutha YTMA 471-4_Scan1-16-bit
subtree: 96 Core, 1 TMA
index built 2026-08-06T13:42:11Z | bytes NOT verified (--include-storage to probe)
vocabularies visible: clinicalomx, clinical_image_roi_object_detection, clinicalomx-pipeline

addressable on 96 child ROI(s): 6 element(s) over 190 distinct store(s)
  ELEMENT   KIND     ARTIFACT         VARIABLE                  USABLE  SHARED
  bgnorm    image    bgnormed         adjusted_image             94/96  bgnormQ
  bgnormQ   image    bgnormed         adjusted_image_post_hoc    94/96  bgnorm
  qc        table    bgnorm_qc        -                          94/96  -
  table     table    cell_tables      -                           0/96  -
  labels    labels   segmentations    -                           0/96  -
  raw       image    source_image     -                          96/96  -

roles carried by those 96 child ROI(s)
  ROLE                  PRESENT  DANGLING  MISSING  DENIED  UNLINKED  OWNER
  core_shapes             96/96         0        -       0         0  clinical_image_roi_object_detection
  roi_thumbnail           94/96         0        -       0         2  clinicalomx-pipeline
      unlinked: ...:TMA0:R7C11, ...:TMA0:R7C12
  bgnormed                94/96         0        -       0         2  clinicalomx-pipeline
  cell_tables              0/96         0        -       0         0  (undeclared)

recipes
  degraded  spatialdata.tma.by-core    SpatialData  merged      1 unit(s)
      cores    <- cores_accepted
      raw      <- source_image             96/96
      bgnorm   <- bgnormed#adjusted_image  94/96
      bgnormQ  <- bgnormed#adjusted_image_post_hoc  94/96
      labels   <- segmentations             0/96  optional, omitted
      table    <- cell_tables               0/96  optional, omitted
      NOTE  the output anchors to this TMA, never to a child
  degraded  spatialdata.tma.per-core   SpatialData  per_child  94 unit(s)
```

How to read it:

- **The first table** lists what a recipe can address. `bgnorm` and `bgnormQ` are two images inside ONE bgnormed zarr, named by `Element.variable`. `SHARED` names the other elements over the same bytes.
- **The second table** lists each role by store. Store counts are over distinct artifacts: 94 normalised cores addressed by two elements are 94 stores, not 188.
- **`recipes`** lists each product, which role fills each element, and how many ROIs have it.

To list what is inside one store per artifact, add `--inspect-stores`:

```text
inside one store per artifact (a sample; nothing asserts the others match it)
  bgnormed  bgnorm/...:TMA0:R0C0.zarr
      adjusted_image           c,y,x          20 x 2048 x 2048       float64   <- bgnorm
      adjusted_image_post_hoc  c,y,x          20 x 2048 x 2048       float64   <- bgnormQ
      labels                   c,y,x          20 x 2048 x 2048       uint8     <- (nothing)
      probs                    c,c_probs,y,x  20 x 3 x 2048 x 2048   float32   <- (nothing)
      coord c                  c              <U8       DAPI, CD3, CD8, ...
      coord signal_units       (scalar)       <U6       counts
```

It reads metadata only and needs `clinicalomx[extract]`.
`labels` in a bgnormed store is the GMM component map from the background model, NOT a cell segmentation.

### What "not available" means

| state | what it means | who to ask |
|---|---|---|
| `undeclared` | nothing installed **here** declares this role at all | the repo that would produce it |
| `unlinked` | the role exists, this ROI has nothing under it | whoever runs that pipeline stage |
| `dangling` | a pointer exists but no Artifact is registered | a broken write; report it |
| `denied` | governance forbids reading those bytes | the data agreement, not the pipeline |
| `missing` | registered, but the file is not in storage | storage, or a deleted file |
| `unplaceable` | the artifact IS present, but nothing on this ROI says where it belongs | usually a declared `needs_features` that nothing populates |

- Do not re-run a pipeline for a `denied` role. The data agreement forbids it.
- `missing` appears only with `--include-storage`. Without it, MISSING prints `-` and `present` prints `present*`.
- `undeclared` describes your environment. A repo that is git-cloned instead of pip-installed declares nothing. The report ends with the vocabularies it could see.

## 3. Plan it

```bash
clinicalomx-extract plan \
  --roi "Arutha YTMA 471-4_Scan1-16-bit:TMA0:R2C4" \
  --target OME-TIFF --dest ./out --out plan.json
```

`plan` writes nothing.
It prints the recipe it chose, the role that fills each element, what it will omit, where each output lands, and a `token`: a content hash of exactly what would be built.

It exits `1` only when something **blocks**.
A degraded plan exits `0`, as `clinicalomx-checks run` does.

Three strictness settings:

- `strict` (default) - any missing required element blocks the whole plan.
- `degrade` - a unit that cannot supply a required element is skipped and reported; optional elements are omitted.
- `best_effort` - build with whatever is there. Nothing chooses this for you.

## 4. Build it

```bash
clinicalomx-extract assemble --plan plan.json --yes
```

`assemble` re-plans and compares tokens.
If the instance changed since you planned, it refuses; run `plan` again.

Outputs go to a **local path** by default.
Pass `--register` to register them as ROI-anchored Artifacts instead.
Registering is governed: `permits_derive` must allow the output folder, and a folder nobody has declared is refused.

## Choose a TMA granularity

```text
spatialdata.tma.by-core     ONE SpatialData for the whole TMA.
                            One image element per core, all in one coordinate system.
                            Anchors to the TMA.

spatialdata.tma.per-core    ONE SpatialData per core. Ninety-six objects.
                            Same inputs, same coverage.
```

- Take the merged one to compare cores or keep their spatial relationship.
- Take the per-core one when each core is an independent sample you will loop over.

A merged object anchors to the **TMA**, never to a core.
From a core, walk up to its parent to find it.

## When a role is not there yet

The segmentation and bgnorm repos do not register their outputs on the instance, so `segmentations`, `cell_tables` and `bgnormed` resolve `undeclared` everywhere.
Recipes name them anyway.
Once those repos register, the same recipes report them as present and include them.

## Reference

- [extract-recipes.md](extract-recipes.md) - every recipe, its elements, and the roles each reads in preference order. Generated.
- `python -m clinicalomx.extract recipes` - the same, plus whatever your environment has installed.
- [src/clinicalomx/extract/](../src/clinicalomx/extract/) - the code. Each module's docstring says what it holds.
