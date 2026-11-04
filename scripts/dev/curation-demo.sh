#!/usr/bin/env bash
# Curate a real spreadsheet on a throwaway database, in the curation review page.

set -euo pipefail
cd "$(dirname "$0")/../.."

# Docker Desktop on macOS: testcontainers mounts the host's socket path into its reaper
# container, and `~/.docker/run/docker.sock` cannot be mounted, so the reaper never starts.
# The socket at this path inside Docker's VM is the same daemon.
if [[ "$(uname)" == Darwin ]]; then
  export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE="${TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE:-/var/run/docker.sock}"
fi

echo "==> preparing the Python environment (the first run downloads the wheels)"
# clinicalomx, the ledger and the reagents are non-editable path installs in the markers project
# and uv does not see an edit under their `src/`, so all three are rebuilt from this tree.
exec uv run --project packages/clinicalomx_markers --group pg --extra api --extra sheet \
  --reinstall-package clinicalomx --reinstall-package clinicalomx_ledger \
  --reinstall-package clinicalomx_reagents \
  python scripts/dev/curation_demo.py "$@"
