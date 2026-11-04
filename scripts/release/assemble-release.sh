#!/usr/bin/env bash
# Materialise one platform release under the /opt release layout:
#
#   /opt/clinicalomx_data_platform/
#     releases/platform-v<N>/     this script creates these, one per tag, immutable
#     current -> releases/...     this script NEVER touches it; the flip is a cutover step
#     shared/runtime/             worker state that must survive release flips (worker-home)
#     shared/{logs,caches,user-data}/
#     config/package-env/         the single authoritative copy of each env file, and of the
#                                 site files: site.yaml and what scripts/site/render.py writes
#                                 from it, site.env and users.conf
#
# Usage:
#   assemble-release.sh <platform-tag> [root]
#     platform-tag  e.g. platform-v1 (must exist on the remote)
#     root          default /opt/clinicalomx_data_platform
#
# Optional environment:
#   PRUNE=0       skip pruning old release trees
#   PRUNE_KEEP    how many newest releases pruning always keeps (default 3)
#

set -euo pipefail

TAG="${1:?usage: assemble-release.sh <platform-tag> [root]}"
ROOT="${2:-/opt/clinicalomx_data_platform}"
# The repository is the site's source.repository, read from the site.env every release links
# unless the caller names it; cloud-init does, at boot, before any site.env exists.
# REPO_URL is overridable only so tests/test-assemble-release.sh can clone a throwaway
# repository; the hub VM never sets it.
if [ -z "${CLINICALOMX_SITE_REPOSITORY:-}" ] && [ -f "$ROOT/config/package-env/site.env" ]; then
    CLINICALOMX_SITE_REPOSITORY="$(sed -n 's/^CLINICALOMX_SITE_REPOSITORY=//p' "$ROOT/config/package-env/site.env")"
fi
REPO_URL="${REPO_URL:-https://github.com/${CLINICALOMX_SITE_REPOSITORY:?no site.env in $ROOT/config/package-env: render site.yaml or set CLINICALOMX_SITE_REPOSITORY}.git}"
DEST="$ROOT/releases/$TAG"
STAGE="$ROOT/releases/.assembling-$TAG"

case "$TAG" in
    platform-v*) ;;
    *) echo "ERROR: '$TAG' is not a platform release tag (expected platform-v<N>)" >&2; exit 2 ;;
esac

if [ -e "$DEST" ]; then
    echo "ERROR: $DEST already exists; a release is immutable, cut a new tag instead" >&2
    exit 1
fi

mkdir -p "$ROOT/releases" "$ROOT/config/package-env" \
    "$ROOT/shared/runtime" "$ROOT/shared/logs" "$ROOT/shared/caches" "$ROOT/shared/user-data"

if [ "${PRUNE:-1}" != 0 ]; then
    "$(dirname "$0")/prune-releases.sh" --apply --keep "${PRUNE_KEEP:-3}" --assembling "$TAG" "$ROOT" \
        || echo "[assemble] WARNING: pruning stopped early or refused; see the [prune] lines above. The clone may not fit; a process scan needs root, so run 'sudo $(dirname "$0")/prune-releases.sh --apply $ROOT' separately" >&2
fi

# A staging tree left by a killed run is never a release, so it is cleared rather than trusted.
rm -rf "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

echo "[assemble] cloning $TAG into $STAGE"
git clone --branch "$TAG" --depth 1 "$REPO_URL" "$STAGE"

# Refuse a clone that is not exactly the tag; --branch on a tag detaches there, and
# anything else means the remote moved under us. The check names the tag it wants and
# compares commits. It never asks git which tag HEAD is: a wheel tag cut at the release
# commit (clinicalomx-v*, clinical-roi-v*) shares HEAD with the platform tag, the clone
# fetches both, and `git describe --exact-match` answers with whichever it prefers.
head="$(git -C "$STAGE" rev-parse HEAD)"
want="$(git -C "$STAGE" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" || true)"
if [ "$head" != "$want" ]; then
    echo "ERROR: clone is at $head, but tag $TAG is at '${want:-<not in clone>}'" >&2
    exit 1
fi

# Every env-file location the runtime definitions read, linked to the one authoritative
# copy under config/package-env/. All eight targets are gitignored in the release tree, so
# git status stays clean. A missing config file is linked anyway (a dangling symlink) and
# reported, so the operator authors it in config/ rather than inside a release.
link() {
    local rel="$1" cfg="$ROOT/config/package-env/$2"
    ln -s "$cfg" "$STAGE/$rel"
    [ -e "$cfg" ] || echo "[assemble] NOTE: $cfg does not exist yet; author it before starting services"
}
link packages/clinicalomxdb/hub-database/.env                                  hub-database.env
link packages/clinicalomxdb/hub-pipelines/.env                                 hub-pipelines.env
link packages/clinicalomxdb/hub-ops/.env                                       hub-ops.env
link packages/clinicalomxdb/hub-pipelines/prefect/worker.env                   worker.env
link packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.env      vm-process.env
link site.yaml                                                                 site.yaml
link site.env                                                                  site.env
link packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf     users.conf

# The one step that makes the release exist. Same filesystem, so the rename is atomic.
mv "$STAGE" "$DEST"
trap - EXIT

echo "[assemble] done: $DEST at $(git -C "$DEST" rev-parse --short HEAD)"
echo "[assemble] current stays untouched: $(readlink "$ROOT/current" 2>/dev/null || echo '<not set>')"
