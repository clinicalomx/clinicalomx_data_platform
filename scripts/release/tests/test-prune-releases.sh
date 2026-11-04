#!/usr/bin/env bash

set -euo pipefail

PRUNE="$(cd "$(dirname "$0")/.." && pwd)/prune-releases.sh"
IMAGE="${IMAGE:-alpine:3.20}"
WORK="$(mktemp -d)"
containers=()
pids=()
cleanup() {
    [ "${#containers[@]}" -eq 0 ] || docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
    [ "${#pids[@]}" -eq 0 ] || kill "${pids[@]}" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

fails=0
pass() { echo "ok   $1"; }
fail() { echo "FAIL $1" >&2; fails=$((fails + 1)); }
exists() { [ -d "$ROOT/releases/$1" ]; }

# A release root: platform-v11..v26, current -> v25 (relative, as the hub VM has it), plus
# shared/ and config/ that must never be touched, and a non-release entry.
fresh_root() {
    ROOT="$WORK/root-$1"
    mkdir -p "$ROOT/releases" "$ROOT/shared/runtime" "$ROOT/config/package-env"
    for n in $(seq 11 26); do
        mkdir -p "$ROOT/releases/platform-v$n/packages/clinicalomxdb/hub-ops/caddy"
        echo ":8080" > "$ROOT/releases/platform-v$n/packages/clinicalomxdb/hub-ops/caddy/Caddyfile"
    done
    mkdir -p "$ROOT/releases/notes"
    ln -s releases/platform-v25 "$ROOT/current"
}

run_container() {  # <name> <host dir> <run|create>
    local id
    if [ "$3" = run ]; then
        id="$(docker run -d --label prune-releases-test=1 --name "$1" -v "$2:/etc/caddy:ro" "$IMAGE" sleep 600)"
    else
        id="$(docker create --label prune-releases-test=1 --name "$1" -v "$2:/etc/caddy:ro" "$IMAGE" sleep 600)"
    fi
    containers+=("$id")
}

# --- 0. off Linux there is no /proc, so the script refuses and removes nothing -------------
if [ ! -d /proc/self ]; then
    fresh_root noproc
    if out="$("$PRUNE" --apply "$ROOT" 2>&1)"; then fail "no /proc did not refuse"
    elif exists platform-v11 && echo "$out" | grep -q 'no /proc'; then pass "no /proc refuses and removes nothing"
    else fail "no /proc refusal"; echo "$out"; fi
    echo "skip: every other case reads /proc, which only Linux has; they run in the release-scripts CI job"
    [ "$fails" -eq 0 ] || { echo "$fails failure(s)" >&2; exit 1; }
    exit 0
fi

# The script as root, so the process scan can read every working directory.
prune() {
    if [ "$(id -u)" = 0 ]; then "$PRUNE" "$@"
    else sudo -n env "DOCKER=${DOCKER:-docker}" "$PRUNE" "$@"; fi
}
if [ "$(id -u)" != 0 ] && ! sudo -n true 2>/dev/null; then
    echo "these tests need root or passwordless sudo, because a full process scan does" >&2
    exit 1
fi

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null
tag="prt-$$"

# --- 1. dry run is the default: the plan is printed and nothing is removed -----------------
fresh_root dry
out="$(prune "$ROOT")"
if echo "$out" | grep -q 'remove  platform-v11' && exists platform-v11 && echo "$out" | grep -q 'dry run'; then
    pass "dry run reports removals and removes nothing"
else fail "dry run"; echo "$out"; fi

# --- 2. the 2026-09-24 incident: a tree that is neither current, previous nor recent, but
# that running containers bind-mount, is KEPT; an otherwise identical unmounted tree goes. ---
fresh_root mounted
run_container "$tag-caddy" "$ROOT/releases/platform-v20/packages/clinicalomxdb/hub-ops/caddy" run
run_container "$tag-grafana" "$ROOT/releases/platform-v20/packages/clinicalomxdb/hub-ops" run
run_container "$tag-stopped" "$ROOT/releases/platform-v16/packages/clinicalomxdb/hub-ops/caddy" create
out="$(prune --apply "$ROOT")"
if exists platform-v20 && echo "$out" | grep -q "keep    platform-v20 (mounted by .*$tag-caddy"; then
    pass "a tree running containers bind-mount is kept and named"
else fail "live mount protection"; echo "$out"; fi
if [ "$(docker exec "$tag-caddy" cat /etc/caddy/Caddyfile)" = ":8080" ]; then
    pass "the mounted config is still readable inside the container"
else fail "container config readable"; fi
if exists platform-v16; then pass "a tree a stopped container mounts is kept"; else fail "stopped container mount"; fi
if ! exists platform-v19 && ! exists platform-v21 && ! exists platform-v11; then
    pass "unmounted old trees beside it are removed"
else fail "control trees removed"; echo "$out"; fi

# --- 3. the stated retention: current, previous, the assembling tree, keep-N newest ---------
fresh_root retention
prune --apply --keep 2 --assembling platform-v13 "$ROOT" >/dev/null
kept="$(cd "$ROOT/releases" && printf '%s\n' * | sort | tr '\n' ' ')"
if [ "$kept" = "notes platform-v13 platform-v24 platform-v25 platform-v26 " ]; then
    pass "keeps current, previous, assembling and the newest two, and leaves non-releases"
else fail "retention: kept '$kept'"; fi
if [ -L "$ROOT/current" ] && [ -d "$ROOT/shared/runtime" ] && [ -d "$ROOT/config/package-env" ]; then
    pass "current, shared/ and config/ are untouched"
else fail "outside releases/ touched"; fi

# --- 4. fail closed when the containers cannot be listed -----------------------------------
fresh_root nodocker
if DOCKER=false prune --apply "$ROOT" >/dev/null 2>&1; then fail "unreadable docker did not refuse"
elif exists platform-v11; then pass "unreadable docker refuses and removes nothing"
else fail "unreadable docker removed a tree"; fi

# --- 5. fail closed when current does not resolve to a release tree ------------------------
fresh_root nocurrent
rm "$ROOT/current"; ln -s releases/platform-v99 "$ROOT/current"
if prune --apply "$ROOT" >/dev/null 2>&1; then fail "dangling current did not refuse"
elif exists platform-v11; then pass "dangling current refuses and removes nothing"
else fail "dangling current removed a tree"; fi

# --- 6. a container mounting through current/ cannot be placed in a tree: refuse ----------
fresh_root viacurrent
run_container "$tag-viacurrent" "$ROOT/current/packages/clinicalomxdb/hub-ops/caddy" run
if out="$(prune --apply "$ROOT" 2>&1)"; then fail "a mount through current did not refuse"
elif exists platform-v11 && echo "$out" | grep -q "container(s) $tag-viacurrent mount a path through"; then
    pass "a mount through current refuses, names the container and removes nothing"
else fail "mount through current"; echo "$out"; fi
docker rm -f "$tag-viacurrent" >/dev/null

# --- 7. a pool worker started under current/ before a flip keeps its tree ----------------
fresh_root cwd
rm "$ROOT/current"; ln -s releases/platform-v20 "$ROOT/current"
( cd "$ROOT/current/packages/clinicalomxdb/hub-ops" && exec sleep 600 ) &
worker=$!
pids+=("$worker")
for _ in $(seq 50); do
    case "$(readlink "/proc/$worker/cwd" 2>/dev/null)" in "$ROOT/releases/platform-v20/"*) break ;; esac
    sleep 0.1
done
rm "$ROOT/current"; ln -s releases/platform-v25 "$ROOT/current"
out="$(prune --apply "$ROOT")"
if exists platform-v20 && echo "$out" | grep -q "keep    platform-v20 (cwd of pid $worker (sleep))"; then
    pass "a tree a process works inside is kept and the process named"
else fail "process cwd protection"; echo "$out"; fi
if ! exists platform-v19 && ! exists platform-v21; then
    pass "unused trees beside it are removed"
else fail "cwd control trees removed"; echo "$out"; fi

# --- 8. as an ordinary user, root's processes cannot be read: refuse ----------------------
fresh_root unreadable
if [ "$(id -u)" = 0 ] || readlink /proc/1/cwd >/dev/null 2>&1; then
    echo "skip: pid 1's working directory is readable here, so there is no unreadable process to refuse on"
elif out="$("$PRUNE" --apply "$ROOT" 2>&1)"; then fail "an unreadable process did not refuse"
elif exists platform-v11 && echo "$out" | grep -q 'cannot read the working directory of pid(s) .*run it with sudo'; then
    pass "an unreadable process refuses, names it and removes nothing"
else fail "unreadable process"; echo "$out"; fi

[ "$fails" -eq 0 ] || { echo "$fails failure(s)" >&2; exit 1; }
echo "all passed"
