#!/usr/bin/env bash
# Extract Explorer, locally.
#
#   ./run_extract_app.sh            # http://localhost:8601, demo data, no database
#   PORT=8602 ./run_extract_app.sh
#   ./run_extract_app.sh --env dev  # Vite dev server, hot reload, two ports
#
# For the same app inside the operator portal's tab shell, run
# `./scripts/dev/control-plane.sh` from the repository root instead.
#
# Demo mode drives the real resolver against a synthetic Snapshot, so the states, the coverage
# numbers and the plan are all produced by production code - only the instance is fabricated.
# Flip "Live instance" in the sidebar to point at the connected instance (which is reachable
# from the control-plane VM or the HPC, not from a laptop - see references/operations.md).
#
# `--env prod` by default, and that is not an optimisation: prod mode is the ONE-PORT arrangement
# the container uses (the compiled frontend is mounted into the backend), so running it here
# exercises the same shape the portal serves. `--env dev` is the Vite dev server on its own port
# with hot reload, which is what you want while editing `extract/app/web/`.
#
# The diagrams need a `dot` binary (`brew install graphviz` / `apt-get install graphviz`). Without
# one the panels print the DOT source and say so rather than quietly drawing nothing.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8601}"
ENV_ARGS=("--env" "prod" "--frontend-port" "${PORT}" "--backend-port" "${PORT}")
if [ "${1:-}" = "--env" ] && [ "${2:-}" = "dev" ]; then
  ENV_ARGS=("--env" "dev" "--frontend-port" "${PORT}")
fi

echo "==> Extract Explorer on http://localhost:${PORT}"
exec uv run --extra app --extra extract reflex run "${ENV_ARGS[@]}"
