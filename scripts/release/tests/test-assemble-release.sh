#!/usr/bin/env bash

set -euo pipefail

ASSEMBLE="$(cd "$(dirname "$0")/.." && pwd)/assemble-release.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fails=0
pass() { echo "ok   $1"; }
fail() { echo "FAIL $1" >&2; fails=$((fails + 1)); }

LINKS=(
    packages/clinicalomxdb/hub-database/.env
    packages/clinicalomxdb/hub-pipelines/.env
    packages/clinicalomxdb/hub-ops/.env
    packages/clinicalomxdb/hub-pipelines/prefect/worker.env
    packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.env
    site.yaml
    site.env
    packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf
)

REPO="$WORK/repo"
git init -q "$REPO"
g() { git -C "$REPO" -c user.name=test -c user.email=test@example.invalid "$@"; }
for rel in "${LINKS[@]}"; do
    mkdir -p "$REPO/$(dirname "$rel")"
    touch "$REPO/$(dirname "$rel")/README"
    echo "$rel" >> "$REPO/.gitignore"
done
g add .
g commit -q -m release

g tag -a platform-v1 -m platform-v1
sleep 1
g tag -a clinicalomx-v9.9.9 -m clinicalomx-v9.9.9

run() { REPO_URL="file://$REPO" PRUNE=0 "$ASSEMBLE" "$@" > "$WORK/out" 2>&1; }

ROOT="$WORK/root"
DEST="$ROOT/releases/platform-v1"
if run platform-v1 "$ROOT"; then pass "assembles when a wheel tag shares the release commit"
else fail "assembles when a wheel tag shares the release commit"; cat "$WORK/out" >&2; fi

if [ -d "$DEST" ] && [ "$(git -C "$DEST" tag --points-at HEAD | wc -l)" -ge 2 ] \
    && [ "$(git -C "$DEST" describe --tags --exact-match)" != platform-v1 ]; then
    pass "the clone carries both tags and describe names the wheel tag (the case is real)"
else fail "the clone carries both tags and describe names the wheel tag (the case is real)"; fi

missing=0
for rel in "${LINKS[@]}"; do [ -L "$DEST/$rel" ] || missing=$((missing + 1)); done
if [ "$missing" -eq 0 ]; then pass "every config link exists"
else fail "every config link exists ($missing missing)"; fi

if [ -z "$(git -C "$DEST" status --porcelain)" ]; then pass "the release tree is clean"
else fail "the release tree is clean"; fi

mkdir -p "$REPO/packages/clinicalomxdb/hub-ops"
echo tracked > "$REPO/packages/clinicalomxdb/hub-ops/.env"
g add -f packages/clinicalomxdb/hub-ops/.env
g commit -q -m collide
g tag -a platform-v2 -m platform-v2

if run platform-v2 "$ROOT"; then fail "a failed link step exits non-zero"
else pass "a failed link step exits non-zero"; fi

if [ ! -e "$ROOT/releases/platform-v2" ] && [ ! -e "$ROOT/releases/.assembling-platform-v2" ]; then
    pass "a failed assembly leaves neither the release nor its staging tree"
else fail "a failed assembly leaves neither the release nor its staging tree"; fi

[ "$fails" -eq 0 ] || { echo "$fails failure(s)" >&2; exit 1; }
echo "all passed"
