# Pipeline Deployment
Registers this directory's pipelines with Prefect on the hub VM, and rolls the review app and the build app to a new image.

Words used here:

- **Prefect** is the scheduler that runs the pipelines.
- A **deployment** is one pipeline registered with Prefect under a name, from `prefect.yaml` in this directory.
- A **block** is a setting or secret stored in Prefect. The deployments read two: `github-croi-pat` and `ghcr-croi`.
- An **automation** is a Prefect rule that starts one deployment when another finishes.

## 0. Requirements
- A shell on the hub VM as `ubuntu`: `packages/clinicalomxdb/deployment.md` steps 1 and 2.
- The three Prefect workers running: `packages/clinicalomxdb/hub-pipelines/prefect/workers/deployment.md`.
- The two GitHub tokens `comx-ci-read` and `comx-ghcr-read` from the lab vault (`infra/secrets/README.md` names their entries).

Every command below runs on the hub VM, as `ubuntu`.

## 1. Open the Prefect CLI
On the hub VM:
```bash
R=/opt/clinicalomx_data_platform
export PREFECT_API_URL=http://localhost:4200/api
source $R/current/packages/clinicalomxdb/hub-pipelines/prefect/.venv/bin/activate
```
A correct result is a prompt that starts with `(.venv)`.
Keep this shell open for every step below.

## 2. Load the site settings
On the hub VM, in the Prefect CLI shell:
```bash
set -a; . $R/current/site.env; set +a
python $R/current/scripts/site/prefect_yaml.py guard
```
A correct result is `every site placeholder in both prefect.yaml files is set`.
A `REFUSED` line names a `site.yaml` key that is empty. Fill it in `$R/config/package-env/site.yaml` and render it (`docs/operations.md` section 8.2, step 2), then repeat this step.

## 3. Store the two GitHub tokens in Prefect
Do `docs/operations.md` section 4.2, step 2, in this shell.
Each token prompt shows nothing while you paste. That is expected.
A correct result is `container-registry-auth/ghcr-croi saved` and `secret/github-croi-pat saved`.

Then clear the tokens from the shell:
```bash
unset COMX_CI_READ COMX_GHCR_READ
```

## 4. Register the deployments
On the hub VM, in the Prefect CLI shell:
```bash
cd $R/current/packages/clinicalomx_pipelines/qptiff_pipeline
prefect deploy --all
```
A correct result registers 29 deployments.

## 5. Register the automations
Run this once, on a Prefect server that has never had them. Skip it on a release deploy.

On the hub VM:
```bash
PREFECT_API_URL=http://localhost:4200/api \
  $R/current/packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.venv/bin/python \
  -m qptiff_pipeline.orchestration.automations
```
A correct result is one `[graph]` line per automation.
A `[graph] skip` line names a deployment that is not registered yet. Do step 4, then repeat this step.

## 6. Check the deployments
On the hub VM, in the Prefect CLI shell:
```bash
prefect deployment ls
prefect deployment inspect 'data-tests/pipeline-data-tests' | grep -E "'branch'|x-access-token"
```
A correct result lists the deployments, and shows `'branch': 'platform-v<N>'` and `'username': 'x-access-token'`.
Ignore a `'branch': None` line.

## 7. Run the data tests
Do `docs/operations.md` section 2.8, step 1.
Both runs must reach `Completed`.

## Update or remove
- A new platform tag re-registers these deployments: `docs/operations.md` section 2.6.
- `prefect deploy` never deletes. To remove a deployment that left `prefect.yaml`, read its full `<flow>/<deployment>` name from `prefect deployment ls`, then:

  ```bash
  prefect deployment delete '<flow>/<deployment>'
  ```

# The review app and the build app
Rolls the review app (`/review/` on the portal) or the build app (`/build/`) on the hub VM to the newest image.

Words used here:

- A **container image** is a program packaged with everything it needs.
- A **registry** is the server that stores images. GitHub's registry is GHCR, at `registry.images` in `site.yaml`.
- GitHub builds a new image on every merge to `main` that changes the app. The build runs in `.github/workflows/img-review-app.yml` and `img-build-app.yml`.

## 0. Requirements
On your own computer:
```text
gh
```
`gh` is the GitHub command line tool. Log it in once with `gh auth login`.

Access:

- Read access to the repository `source.repository` on GitHub.
- A shell on the hub VM as `ubuntu`: `packages/clinicalomxdb/deployment.md` steps 1 and 2.

## 1. Check the new image is built
On your own computer:
```bash
gh run list --repo <source.repository> --workflow img-review-app.yml --limit 3
```
Use `img-build-app.yml` for the build app.
A correct result shows the newest run as `completed` and `success`.

To build one without a code change:
```bash
gh workflow run img-review-app.yml --repo <source.repository>
```

## 2. Ask everyone using the app to save their work and close it
Step 3 restarts the app.

## 3. Roll the app to the new image
On the hub VM:
```bash
R=/opt/clinicalomx_data_platform
set -a; . $R/current/site.env; set +a
bash $R/current/packages/clinicalomx_pipelines/qptiff_pipeline/deploy_review_app.sh
```
Use `deploy_build_app.sh` for the build app.
A correct result prints the image list twice, the last 20 log lines, and `>> Done.`

## 4. Check the app
Open the portal (`packages/clinicalomxdb/deployment.md` step 7).
Browse to `http://localhost:8080/review/` or `http://localhost:8080/build/`, with the trailing slash.
A correct result is the app's page, not an error.

## 5. Roll back
On your own computer, read the commit of the image before the newest:
```bash
gh run list --repo <source.repository> --workflow img-review-app.yml --status success --limit 2 --json headSha -q '.[1].headSha'
```
On the hub VM, deploy that image:
```bash
set -a; . $R/current/site.env; set +a
TAG=<commit printed above> bash $R/current/packages/clinicalomx_pipelines/qptiff_pipeline/deploy_review_app.sh
```
A correct result prints `pinned this run to <commit>`.
