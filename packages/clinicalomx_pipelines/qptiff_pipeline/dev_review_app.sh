#!/usr/bin/env bash
# Run the review app LOCALLY on fake data, for UI iteration: no lamindb, no Prefect, no HPC.
#
#   ./dev_review_app.sh            # opens http://localhost:8599, live-reloads on save
#   PORT=8600 ./dev_review_app.sh
#
# Edit src/qptiff_pipeline/review/app.py, save, and the tab refreshes (Streamlit's
# runOnSave + the reload shim in review/dev/app.py). Fake data lives in review/dev/fixtures.py.
#
# First run creates .venv-review-dev (UI + geometry deps only, no lamindb/prefect/DL stack),
# which is why this stays fast and why the fakes are needed at all.
set -euo pipefail
cd "$(dirname "$0")"

# The venv lives at the PACKAGE root (beside pyproject.toml), where the root-anchored
# .dockerignore keeps its ~800MB out of every image build context.
VENV=../.venv-review-dev
PORT="${PORT:-8599}"

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "==> Port $PORT is already in use, so Streamlit cannot have it." >&2
  echo "    Stop what holds it, or set PORT to a free port." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/streamlit" ]; then
  echo "==> creating $VENV (UI deps only)"
  uv venv "$VENV" --python 3.11
  VIRTUAL_ENV="$VENV" uv pip install \
    streamlit streamlit-sortables pillow pydantic \
    "numpy>=1.26" "pandas>=2.0" "scipy>=1.11" "shapely>=2.0" "geopandas>=1.1"
  # The package itself, WITHOUT extras: we only need the modules on sys.path, and its real deps
  # (lamindb/prefect) are exactly what the fakes replace. pyproject.toml is one level up.
  VIRTUAL_ENV="$VENV" uv pip install --no-deps -e ..
  # The domain tier imports clinicalomx.names (since a04f04a). Install the sibling checkout
  # --no-deps: names is import-free, and the local path needs no index credential.
  VIRTUAL_ENV="$VENV" uv pip install --no-deps -e ../../clinicalomx
fi
# watchdog is what runOnSave needs to see review/app.py (see the folderWatchList note below).
# Checked apart from the block above so a venv built before it gets it too.
[ -x "$VENV/bin/watchmedo" ] || VIRTUAL_ENV="$VENV" uv pip install watchdog

echo "==> review app (dev/fake data) on http://localhost:$PORT"
# Open the tab once Streamlit answers. Not --server.headless false: that path asks for an email
# on first run and blocks the terminal. Bounded, so a failed start leaves no waiter behind.
opener="$(command -v open || command -v xdg-open || true)"
if [ -n "$opener" ]; then
  script_pid=$$
  ( for _ in $(seq 300); do
      kill -0 "$script_pid" 2>/dev/null || exit 0
      curl -sf -o /dev/null "http://localhost:$PORT/_stcore/health" && exec "$opener" "http://localhost:$PORT"
      sleep 1
    done ) &
fi
# Streamlit watches only modules under the entry script's folder (review/dev/), so review/app.py
# is watched through folderWatchList. That needs the watchdog watcher: the polling one reads a
# directory's mtime, which an in-place save does not change. The folder watch also skips
# Streamlit's dot-folder blacklist, so a checkout under a hidden directory still reloads.
exec "$VENV/bin/streamlit" run src/qptiff_pipeline/review/dev/app.py \
  --server.fileWatcherType watchdog \
  --server.folderWatchList "$PWD/src/qptiff_pipeline" \
  --server.port "$PORT" \
  --server.address 127.0.0.1 \
  --server.headless true \
  --server.runOnSave true \
  --browser.gatherUsageStats false
