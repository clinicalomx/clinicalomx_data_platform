# The ROI layer

The functions that link each **Artifact** to the **ROI Record** it describes, and read that link back.

## The rule

> Every Artifact **annotates** exactly one ROI Record (`Core`, `TMA`, `Tile`, `WholeSlide`, ...).

The link is stored on both sides:

- on the **ROI Record**: a *role* feature (`segmentations`, `bgnormed`, `source_image`) pointing at the Artifact(s). A role is multi-valued, and `annotate` appends.
- on the **Artifact**: the back-reference `roi_uid`, plus `roi_name` and `roi_type` as convenience copies.

The back-references are single-valued.
`roi.one_roi_per_artifact` checks this.
An artifact shared by many ROIs belongs on their parent.

## Patients

- An analysis-grade ROI carries exactly one patient.
- An ROI holding several patients is a container. It carries none of its own; its patients are the union of its children's, read, not stored.
- There is no stored edge from Patient back to ROI. Query instead.
- The assignment is a claim in the ledger, and `patient` on the Record is its projection.
- `clinicalomx_omop.link` is the writer. It projects a patient onto an ROI only from accepted `patient` ledger claims.

`roi.one_patient_per_roi` and `roi.patient_agrees_with_the_parent` check this.
The full rules are in [the ROI-to-Patient decision record](../../../docs/architecture/decisions/2026-09-01-roi-to-patient-relation.md).

## Names

```text
WholeSlide   <slide stem>                 Arutha YTMA 471-4_Scan1-16-bit
TMA          <slide>:TMA<t>               <slide>:TMA0
Core         <slide>:TMA<t>:R<r>C<c>      <slide>:TMA0:R2C4
```

`get_or_create` is keyed by name, so every repo must produce exactly these names.
`roi.names_are_unique` and `roi.parent_matches_the_name_spine` check them.

## Link outputs to ROIs

Call the function that matches what your transform does to ROIs:

```python
import clinicalomx as cx

# 1:1 - the output annotates the SAME ROI as the input. The common case.
cx.update(input_art, output_art, "segmentations")

# 1:N - a parent ROI fans out to children: TMA -> cores, image -> tiles.
cx.split(tma_art, [(core_roi, core_art), ...], "image")

# N:1 - many ROIs collapse into one: N cores -> a merged dataset.
cx.merge([core_art_1, core_art_2], merged_roi, "merged", merged_artifact=merged_art)

# ->1 - a fresh ROI the caller has already built and saved.
cx.create(core_roi, core_art, "image", parent=tma_roi)

# resolve, or enforce
roi = cx.resolve_roi(artifact)      # the ROI, or None
roi = cx.require_roi(artifact)      # or raise; do not ship an unanchored artifact
```

- `split` takes `(child_roi_record, artifact_or_None)` pairs and returns `(parent_roi, [child_roi, ...])`.
- `merge` links each input's ROI as a `parent_roi` of the target and returns `(target_roi, [source_roi, ...])`.
- `annotate` is idempotent: re-running adds no duplicate link, and heals a half-written one.
- `annotate` does not accept a `None` artifact. Check for `None` at the call site.

### Inputs made before this package

Name the old features your upstream wrote:

```python
cx.update(bgnorm_art, seg_art, "segmentations",
          legacy_uid_features=("source_core_uid",),
          legacy_name_features=("source_core_name",))
```

`resolve_roi` tries `roi_uid` first, then the legacy uid features in order, then the name features.
Every function here takes the same two arguments.

## Create ROI Records and roles

Build and save the ROI Record yourself; this package wires the links.
Two helpers do the get-or-create and typing:

```python
cx.ensure_types({"MyRoiKind": "What it is."}, umbrella=cx.TYPES.ROI)
roi = cx.get_or_create("<slide>:TMA0:R0C0", cx.TYPES.CORE, features={"row": 0, "col": 0})

cx.ensure_features({"segmentations": (ln.Artifact, "What this artifact is to the ROI.")})
```

`ensure_features` always covers the ROI back-references and `parent_roi`.
It accepts a dtype or a `(dtype, description)` pair.
Always pass the pair: an `ln.Feature` cannot be deleted once used.
For anything beyond a one-off, declare it in a `Vocabulary` instead: see [extending.md](extending.md).

## Read it back

These take the feature VALUE, not the ROI and the role:

```python
value = roi.features.get_values().get("segmentations")

art  = cx.newest_artifact(value)   # the current one, by created_at
arts = cx.all_artifacts(value)     # every version, for teardown or relocation
art  = cx.as_artifact(value)       # one value -> Artifact
```

A value comes back in one of three shapes: an `ln.Artifact`, its KEY as a bare string, or a list or set of either.
Use these functions instead of unwrapping it yourself.
They also read the foundation's own `cat[Artifact]` features (`source_image`, `source_artifact`, `roi_parquet`).

`clinicalomx.geometry` finds a child ROI's row in a geometry parquet, by `roi_id` first and `parquet_row` second.
