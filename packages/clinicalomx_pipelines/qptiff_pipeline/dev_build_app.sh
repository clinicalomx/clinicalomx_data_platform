#!/usr/bin/env bash
# Run the Build app's UI LOCALLY on demo data, for UI iteration - no lamindb, no Prefect, no HPC.
#
#   ./dev_build_app.sh            # opens http://localhost:8601, hot-reloads on save
#   PORT=8602 ./dev_build_app.sh
#
# The Build app's screen IS clinicalomx's Extract Explorer (build_app.py is page chrome over it),
# so the code you edit is packages/clinicalomx/src/clinicalomx/extract/app/web/. This serves that
# Explorer standalone against its demo Snapshot (extract/app/demo.py) in Reflex's dev mode, which
# recompiles on save and pushes the change to the open tab. The sibling package's own
# run_extract_app.sh does the work; this only picks dev mode and opens the browser.
#
# First run: uv resolves the app extras and Reflex fetches bun, so it needs a network once.
set -euo pipefail
cd "$(dirname "$0")/../../clinicalomx"

export PORT="${PORT:-8601}"
URL="http://localhost:$PORT"

refuse_if_held() {
  if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
    echo "==> Port $1 is already in use, so Reflex cannot have it." >&2
    echo "    Stop what holds it$2." >&2
    exit 1
  fi
}
refuse_if_held "$PORT" ", or set PORT to a free port"
refuse_if_held 8000 " (Reflex's dev backend port)"

# Open the tab once the frontend answers. Bounded, so a failed start leaves no waiter behind.
opener="$(command -v open || command -v xdg-open || true)"
if [ -n "$opener" ]; then
  script_pid=$$
  ( for _ in $(seq 600); do
      kill -0 "$script_pid" 2>/dev/null || exit 0
      curl -sf -o /dev/null "$URL" && exec "$opener" "$URL"
      sleep 1
    done ) &
fi

exec ./run_extract_app.sh --env dev
