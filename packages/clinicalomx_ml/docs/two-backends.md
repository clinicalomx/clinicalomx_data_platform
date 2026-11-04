# Training with YOLO or RF-DETR

`roi_detection` trains its detectors with either of two frameworks, and one config key picks which.
Use this page to switch framework, run a training job, and know what has been proven.

- [Ultralytics YOLO](https://docs.ultralytics.com) is the default.
- [RF-DETR](https://github.com/roboflow/rf-detr) is Roboflow's DETR detector.

Words used below:

- **HPC**: the shared compute cluster where every GPU job runs.
- **SLURM**: the HPC's job queue. Each training run is one SLURM job.
- **Prefect deployment**: a named job registered on the platform's Prefect server. `prefect deployment run` starts one.
- **MLflow**: the experiment tracker. A training run logs its metrics and its model there.
- **lamindb**: the platform's data catalogue. A registered model is a lamindb Artifact.

## Current state

| | YOLO | RF-DETR |
|---|---|---|
| Trains on the HPC | yes | yes |
| Registers a model | yes, `roi-core` v8 | yes, `roi-core` v9 |
| Scored by the shared evaluator | yes | no |

See [What is not proven for RF-DETR](#what-is-not-proven-for-rf-detr) before you rely on an RF-DETR model.

## How the switch works

One key picks the framework: `parameters.framework`.
Both halves of the pipeline read it:

| Half | Module | Table | Keys |
|---|---|---|---|
| training | `roi_detection/src/croi/detection/backends/training.py` | `TRAINERS` | `ultralytics`, `rfdetr` |
| inference | `roi_detection/src/croi/detection/backends/__init__.py` | `DETECTORS` | `ultralytics`, `rfdetr` |

- A config with no `framework` key trains YOLO, because `backends.DEFAULT_FRAMEWORK` is `ultralytics`.
- Training records the framework on the model it registers: the `model_framework` feature on the lamindb Artifact, and a tag on the MLflow run.
- `registry.load_champion` reads that record back, so whoever loads a champion gets the right backend without naming it.
- A registered champion that records no framework is refused, never defaulted.
- The `train:` block goes to the framework's own `train()` exactly as written. Each config uses its own framework's keyword names, and nothing translates between them.

## The two configs

Both live in `roi_detection/src/croi/objectives/tma_core/configs/` and ship inside the wheel:

- `3_train_and_eval.yaml` trains YOLO.
- `3_train_and_eval_rfdetr.yaml` trains RF-DETR.

Their `data_dir`, `inputs`, `outputs` and `eval` blocks are identical.
So both train on the same splits, register under the same `roi-core` model, and are scored by the same evaluator.
Only `parameters` differs:

| Setting | YOLO | RF-DETR | Why |
|---|---|---|---|
| framework | absent | `framework: rfdetr` | the one switch; absent means `ultralytics` |
| base checkpoint | `ultralytics_model_name: yolo26x.pt` | `rfdetr_model_name: rf-detr-nano.pth` | each key names its framework, so an older config stays valid |
| run name | `yolo26x_roi_core` | `rfdetr_nano_roi_core` | a distinct name gives a distinct run directory, so the two never resume into each other |
| input size | `imgsz: 640` | `resolution: 640` | the same number in each framework's keyword |
| batch | `batch: 16` | `batch_size: 8`, `grad_accum_steps: 2` | the same effective batch of 16 |
| workers | `workers: 4` | `num_workers: 0` | see [A crash after the fit](#a-crash-after-the-fit-loses-rf-detrs-metrics) |
| multi-scale | absent | `multi_scale: false` | without it, a 640 config trains at 800 px and random 480 to 800 px batches |
| early stop | `patience: 30` | `early_stopping: true`, `early_stopping_patience: 30` | keyword only |
| device | `device: 0` | `device: cuda` | each framework's own spelling |
| augmentation | histology-tuned block | none | RF-DETR uses its own defaults |
| `mlflow_log_period` | `25` | absent | YOLO pushes artifacts every N epochs; RF-DETR logs at the end |
| `eval.imgsz` | `640` | `640` | keep it equal to `resolution`: an RF-DETR checkpoint does not record its training size |

Two checks on any RF-DETR run:

- The log line `Using multi-scale training` must be absent. If it appears, the model trained at a different size than production infers at (640 px).
- `expanded_scales: false` alone is not a fix. It trains at 768 px, with 544 to 768 px per batch.

Any RF-DETR versus YOLO comparison made before `multi_scale: false` is biased: RF-DETR trained at 800 px and was evaluated at 640 px.

## Run a training job

Both frameworks run on the `train-core` deployment, which is one A100 GPU on the HPC.
The `config` parameter picks the file, and `name` and `epochs` override the config.

```bash
# any shell whose Prefect CLI points at the hub

# YOLO: the default config
prefect deployment run 'core-train-and-eval/train-core' \
  -p name=demo_yolo26x_roi_core_e30 -p epochs=30

# RF-DETR: the same command, plus the config name
prefect deployment run 'core-train-and-eval/train-core' \
  -p config=3_train_and_eval_rfdetr.yaml \
  -p name=demo_rfdetr_nano_roi_core_e30 -p epochs=30
```

Off the cluster, the same choice is:

```bash
python -m croi tma_core train
python -m croi tma_core train --config 3_train_and_eval_rfdetr.yaml
```

To pick the GPU for one run, override its SLURM resources on the run, not on the deployment:

```bash
--job-variable 'gpu="l40s"' --job-variable 'partition="gpu_cuda"' \
--job-variable 'qos="gpu"' --job-variable 'walltime="03:00:00"'
```

With `epochs=30` and `patience: 30`, neither framework can stop early.
That is a property of the short run, not of the config.

## Read the results

From a workstation, open MLflow through the operator tunnel at `http://127.0.0.1:8080/mlflow`.

The logs print the hub's public MLflow address instead (`MLFLOW_EXTERNAL_TRACKING_URI` in `site.env`).
That address answers only on the hub itself, so from anywhere else it times out and looks like MLflow is down.

Ultralytics prints its per-epoch table to stdout, so the Prefect flow log does not show it.
MLflow holds one `metrics/mAP50-95B` value per epoch.

## Demo results

One 30-epoch run of each framework, untuned, to show the path works.
This is not a model comparison.

Data: `clinical-roi/core/splits/{train,val,test}`, 53 / 12 / 14 images over 27 / 8 / 7 slides.

### YOLO

- MLflow run `demo_yolo26x_roi_core_e30_20260930_151321`, experiment `clinical-roi/core-train`.
- Registered as `models/core/roi-core_v8_yolo26x.pt`, MLflow `roi-core` version 8.
- The alias did not move: training registers and never promotes.

The shared evaluator (`croi.evaluation.evaluate`, `conf: 0.25`, 640 px) on the 14 test images:

| IoU | recall | precision |
|---|---|---|
| 0.5 | 0.876 | 0.921 |
| 0.7 | 0.859 | 0.902 |
| 0.9 | 0.368 | 0.387 |

The median IoU of matched boxes is 0.890.
Over all 79 images, recall is 0.894 and precision 0.922 at IoU 0.5.

Ultralytics' own validator:

| Split | Weights | mAP@50 | mAP@50:95 |
|---|---|---|---|
| val (12 images) | epoch 30 | 0.847 | 0.574 |
| val (12 images) | `best.pt` | 0.831 | 0.600 |
| test (14 images) | `best.pt` | 0.867 | 0.624 |

On test, `best.pt` also scored precision 0.789 and recall 0.824.

### RF-DETR

- MLflow run `demo_rfdetr_nano_roi_core_e30_20261007_212350`. It is marked FAILED and holds parameters only, although all 30 epochs ran.
- The curve is in the stderr of SLURM job 29401965 and in `metrics.csv` in the run directory on scratch.

RF-DETR's own validator, val split (12 images), EMA weights, at epoch 30:

| mAP@50:95 | mAP@50 | mAP@75 | precision | recall |
|---|---|---|---|---|
| 0.602 | 0.804 | 0.715 | 0.762 | 0.827 |

The last epoch was the best, and the curve was still rising slowly.

### Do not compare the two validators

- RF-DETR scores EMA weights with its own COCO-style evaluator.
- Ultralytics scores the model's own weights with a different mAP implementation.

Subtracting one from the other means nothing.
Only the shared evaluator, `evaluate_model`, gives comparable numbers, and RF-DETR has not been through it.

## Known problems

### RF-DETR needs a C compiler in the training image

RF-DETR's box matcher uses Triton on a CUDA GPU, and Triton compiles a small C stub the first time it runs.
`clinical-roi` 0.3.1 added `gcc` and `libc6-dev` to `roi_detection/containers/base.Dockerfile`.

An older image fails about 23 seconds into training with:

```text
RuntimeError: Failed to find C compiler. Please specify via CC environment variable ...
```

The flow reports `DataLoader worker ... exited unexpectedly` instead.
That is a side effect of the first error, not the cause.

Only a Linux CUDA run proves the GPU path:

- `roi_detection/tests/test_detector_seam.py` stubs every framework, so it never runs RF-DETR.
- On a CPU, or a GPU below compute capability 8.0, the matcher falls back to SciPy, which needs no compiler.
- Every GPU offered here (the A100, its MIG slices and the L40S) is 8.0 or higher.

### A crash after the fit loses RF-DETR's metrics

`rfdetr.Trainer` copies RF-DETR's `metrics.csv` into MLflow only after the fit returns.
If anything raises after the last epoch, the MLflow run holds parameters only, and nothing after the fit runs: no test pass, no registration, no evaluation.

This happened once.
After a complete fit, Lightning's teardown found a loader worker had exited with status 1, and raised `Please call iter(combined_loader) first.`
The worker's own error was not recoverable, so the cause is unknown.
Only RF-DETR trains through Lightning (`pytorch_lightning` 2.6.6, the locked version), so YOLO is not affected.

Both RF-DETR configs now set `num_workers: 0`, so no worker process exists to die and a loader error raises with its own traceback.
That costs about 2 s an epoch for `tma_core`, against its ~6 s epochs on the cluster.
For `tissue_objects` loading costs 7 to 9 s an epoch, and its RF-DETR epoch time is unmeasured.
This is unproven: the next fresh RF-DETR fit on the cluster is the test.

### A failed run resumes under the same name

A run directory with `last.ckpt` and no `fit_complete` marker is unfinished.
With `resume: auto`, the next run under the same `name` resumes it rather than training fresh.

### Framework stamps on older models (fixed)

lamindb's `set_values` replaces every external value on an artifact.
`stamp_mlflow_features` called it after `stamp_framework`, which erased the `model_framework` stamp on every champion registered with MLflow.
So `roi-core` v9, an RF-DETR model, was evaluated with the YOLO loader and failed with `AttributeError: 'dict' object has no attribute 'float'`.

The fix adds the value instead of setting it, and refuses a model with no stamp.
Restamp models registered before the fix:

```bash
python -m croi.utils.backfill_model_framework
```

`roi_detection/tests_pg/test_framework_stamp.py` proves both halves on a real instance.

### The MLflow pyfunc ignores `params` (both frameworks)

`croi.detection.packaging.log_detector_model` logs the checkpoint as an MLflow pyfunc with no signature.
Without a params schema, MLflow ignores `params` at predict time and logs a warning:

```python
# a version logged after the croi shared layer; v8 and older cannot load this way
model = mlflow.pyfunc.load_model("models:/roi-core/<version>")
model.predict(image, params={"conf": 0.5})   # conf is IGNORED, the backend default applies
```

In a local test, the pyfunc returned 0 boxes at `conf=0.01` and the direct backend returned 32.
The `general` copy has the same code.
Production loads through `registry.load_champion`, so this blocks nothing.
Anyone who loads the model through MLflow gets the defaults silently.

## What is not proven for RF-DETR

- RF-DETR's own test-split pass has never run.
- `evaluate_model` has never scored an RF-DETR model: there are no recall or precision numbers at IoU 0.5, 0.7 or 0.9, and no contact sheet.
- No registered RF-DETR champion has been loaded and run with the RF-DETR backend.
  The only RF-DETR prediction through `rfdetr.Detector` is a local CPU test, on a checkpoint renamed to the platform's `.pt` key.

RF-DETR training works on the cluster.
The full pipeline, from registered model to scored predictions, is not yet proven for it.
