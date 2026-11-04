#!/usr/bin/env bash
# Register both Prefect projects' deployments with this site's values.
#
#   scripts/site/deploy.sh [site.yaml]
#
# 1. Render site.yaml to site.env and users.conf (scripts/site/render.py), refusing a bad file.
# 2. Source site.env, so `prefect deploy` substitutes each {{ $NAME }} in both prefect.yaml files.
# 3. Refuse while any placeholder would survive. 
# 4. `prefect deploy --all` in each project, exactly as docs/operations.md section 2.6 runs it.
#
# Needs uv, PREFECT_API_URL pointing at the real server, and `prefect` on PATH for the croi
# project: on the hub, activate the shared worker venv
# (packages/clinicalomxdb/hub-pipelines/prefect/.venv) first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${PREFECT_API_URL:?point PREFECT_API_URL at the Prefect server, e.g. http://localhost:4200/api}"

uv run --quiet "$ROOT/scripts/site/render.py" "$@"
set -a
# shellcheck source=/dev/null
. "$ROOT/site.env"
set +a
uv run --quiet "$ROOT/scripts/site/prefect_yaml.py" guard

(cd "$ROOT/packages/clinicalomx_pipelines/qptiff_pipeline" && uv run --no-sync prefect deploy --all)
(cd "$ROOT/packages/clinicalomx_ml/roi_detection" && prefect deploy --all)
