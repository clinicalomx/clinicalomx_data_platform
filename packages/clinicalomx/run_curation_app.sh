#!/usr/bin/env bash
# The curation review page, with the curation API v1 served beneath it on the same port.
#
#   CURATION_TOKENS=tokens.json CURATION_BRANCH=curation ./run_curation_app.sh   # page + API on :8602
#
# `clinicalomx_markers.api.serve` is the app: it mounts the page beneath `curation_api`, whose
# token digest file and branch are the two variables above, and it writes to whichever lamin
# instance this machine connects to. For a throwaway instance curating a real spreadsheet, use
# `scripts/dev/curation-demo.sh <sheet>` from the repository root instead.
#
# The page reaches the API over HTTP at CURATION_API_URL, which defaults to this process.
# Each curator enters their own token in the page, since the token is what names the actor to
# the ledger. There is no shared default: with no token entered, the page refuses a decision.
set -euo pipefail
cd "$(dirname "$0")/curation_app"
PORT="${PORT:-8602}"
export CURATION_API_URL="${CURATION_API_URL:-http://localhost:${PORT}}"
# The markers project, because the API is its; `--group app` brings Reflex. clinicalomx, the
# ledger and the reagents are non-editable path installs there, and uv does not see an edit
# under their `src/`, so all three are rebuilt from this tree rather than served from a stale copy.
exec uv run --project ../../clinicalomx_markers --extra api --group app \
  --reinstall-package clinicalomx --reinstall-package clinicalomx_ledger \
  --reinstall-package clinicalomx_reagents \
  reflex run --env prod --frontend-port "${PORT}" --backend-port "${PORT}"
