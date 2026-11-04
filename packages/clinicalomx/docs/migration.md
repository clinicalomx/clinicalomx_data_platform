# Migrating to clinicalomx

Two independent migrations.
A repo can do the first and stop.

1. **Dependency:** replace `dataspec` and `roispec` with `clinicalomx`. Both old names were deleted at 0.3.0.
2. **Vocabulary ownership:** declare the names only your repo uses in your repo, instead of in the foundation.

## 1. Dependency

The dependency migration is done inside this monorepo.
No package imports `dataspec` or `roispec`.

For a repo outside it:

1. Rewrite the imports with this map:

   | Old | New |
   |---|---|
   | `dataspec` | `clinicalomx` |
   | `dataspec.<config, environments, idempotency, manifest, models, refs, review, settings>` | `clinicalomx.<same name>` |
   | `roispec` | `clinicalomx` |
   | `roispec.ops` | `clinicalomx.roi` |

2. Then widen the pin to 0.3.0.

[upgrading-to-v0.3.0.md](upgrading-to-v0.3.0.md) has the full detail.

## 2. Vocabulary ownership

The foundation holds 20 active features plus 1 deprecated: the names the framework itself reads or writes, plus the shared conventions no single repo owns.
Every other name is declared by its owner through `clinicalomx.vocab.Vocabulary` and the `clinicalomx.vocabulary` entry point.
[extending.md](extending.md) shows how.

| Family | Owner | Examples |
|---|---|---|
| ROI relations, gate state, environment pointers, clinical spine links, provenance, ROI geometry interchange | **clinicalomx** (foundation) | `roi_uid`, `pipeline_state`, `docker_ref`, `patient_id`, `source_artifact`, `roi_parquet` |
| bgnorm bookkeeping, qptiff source metadata, the ROI review cycle, image typing | **clinicalomx-pipeline** | `bgnorm_attempts`, `bgnorm_deferred`, `qptiff_dims`, `roi_thumbnail`, `roi_accepted`, `image_type` |
| Detection geometry and outputs | **clinical_image_roi_object_detection** | `bbox_x1`, `row`, `col`, `is_detected`, `detection_confidence`, `core_completeness`, `parquet_row`, `detections`, `tma_shapes`, `core_shapes` |
| Segmentation outputs | **segmentation** | `segmentations`, `cell_tables`, `cell_anndata` |
| Imaging-marker identity and roles | **clinicalomx_markers** | `segmentation_role`, `targets`, `subcellular_location`, `uniprot_release`, `marker_state` |
| Clinical covariates | **patient_metadata_etl_agent** | `age`, `sex`, `histology`, `ALK_mutation`, `clinical_benefit`, `death`, `cpid` |

Two families have no owner yet:

- **`bgnormed` / `bgnormed_image`**: produced by bgnorm, read by the pipeline and segmentation. Assign them to the pipeline or to the foundation; never let both repos declare them.
- **Pyramid and image metadata** (`pyramid_level`, `decimation_step`, `smallest_pyramid_h/w`, `scale_factor`, `height_px`, `width_px`, `pixel_size_um`, `n_channels`, `channel_dim_size`, `projection_mode`): written during ingest, read by several repos. Check whether clinical-roi reads them before assigning them.

A name nobody claims stays an unowned extra; nothing breaks.

Today `clinicalomx_pipelines` declares a vocabulary, checks and governance policies, and `clinicalomx_markers` declares a vocabulary.
For the current state, run this against the instance:

```bash
python -m clinicalomx.vocab audit
```

## Provision a fresh instance

Once every consumer declares its own vocabulary, a new instance can be built from declarations alone:

```bash
python -m clinicalomx.vocab apply --with-consumers
```

This applies the foundation plus every installed consumer's `Vocabulary`.
Plain `apply` touches only the foundation.

To test that the declarations are sufficient:

1. Create an empty lamindb instance.
2. Run `apply --with-consumers` against it.
3. Run a pipeline stage.
