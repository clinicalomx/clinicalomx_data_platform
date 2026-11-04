# ROI Detection Deployment
Registers this directory's ROI detection pipelines with Prefect on the hub VM.

Words used here:

- **Prefect** is the scheduler that runs the pipelines.
- A **deployment** is one pipeline registered with Prefect under a name, from `prefect.yaml` in this directory.

## 0. Requirements
- The pipeline deployments registered first: `packages/clinicalomx_pipelines/qptiff_pipeline/deployment.md`. Its steps 1 to 3 open the Prefect CLI, load the site settings and store the GitHub tokens these deployments also use.

Every command below runs on the hub VM, as `ubuntu`, in the Prefect CLI shell from that guide.

## 1. Register the deployments
On the hub VM:
```bash
cd $R/current/packages/clinicalomx_ml/roi_detection
prefect deploy --all
```
A correct result registers 5 deployments.

## 2. Check the deployments
On the hub VM:
```bash
prefect deployment ls
```
A correct result lists `train-general`, `train-core`, `utils-recover-annotated-images`, `ingest-labels-core` and `split-core`, each under its flow name.

## Update or remove
- A new platform tag re-registers these deployments: `docs/operations.md` section 2.6.
- A new image for them: `containers/deployment.md`.
- `prefect deploy` never deletes. To remove one, read its full `<flow>/<deployment>` name from `prefect deployment ls`, then:

  ```bash
  prefect deployment delete '<flow>/<deployment>'
  ```
