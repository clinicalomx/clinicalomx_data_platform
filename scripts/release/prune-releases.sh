#!/usr/bin/env bash
# Remove old platform release trees under <root>/releases
#
# Usage:
#   prune-releases.sh [--apply] [--keep N] [--assembling <tag>] [root]
#     root          default /opt/clinicalomx_data_platform

set -euo pipefail

APPLY=0
KEEP=3
ASSEMBLING=""
ROOT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --keep) KEEP="${2:?--keep needs a number}"; shift ;;
        --assembling) ASSEMBLING="${2:?--assembling needs a tag}"; shift ;;
        -*) echo "ERROR: unknown option '$1'" >&2; exit 2 ;;
        *) ROOT="$1" ;;
    esac
    shift
done
ROOT="${ROOT:-/opt/clinicalomx_data_platform}"
DOCKER="${DOCKER:-docker}"
RELEASES="$ROOT/releases"

case "$KEEP" in
    ''|*[!0-9]*) echo "ERROR: --keep must be a whole number, got '$KEEP'" >&2; exit 2 ;;
esac

removed=()
say() { echo "[prune] $*"; }
refuse() {
    if [ "${#removed[@]}" -eq 0 ]; then
        echo "[prune] REFUSED: $*; nothing removed" >&2
    else
        echo "[prune] REFUSED: $*; stopped after removing ${removed[*]}" >&2
    fi
    exit 1
}

[ -d "$RELEASES" ] || { say "no $RELEASES; nothing to prune"; exit 0; }

# The version number of a platform-v<N> name, or nothing for any other name.
version_of() {
    case "$1" in
        platform-v*[!0-9]*|platform-v) return 0 ;;
        platform-v*) echo "${1#platform-v}" ;;
    esac
}

# The release tag `current` points at. A relative or absolute link both resolve, and one that
# names anything but an existing releases/platform-v<N> tree is a refusal: the live tree must
# be known before anything is judged removable.
current_target="$(readlink "$ROOT/current" 2>/dev/null)" || refuse "$ROOT/current is not a symlink"
CURRENT="$(basename "$current_target")"
if ! { [ -d "$RELEASES/$CURRENT" ] && [ -n "$(version_of "$CURRENT")" ] \
    && [ "$(cd "$ROOT/current" && pwd -P)" = "$(cd "$RELEASES/$CURRENT" && pwd -P)" ]; }; then
    refuse "current -> $current_target does not resolve to a release tree under $RELEASES"
fi

REAL_ROOT="$(cd "$ROOT" && pwd -P)"

# Sets IN_USE to one "<kind>\t<label>\t<path>" line per container mount (kind mount, label the
# container name) and per host process working directory (kind cwd, label "pid N (comm)").
# Every way this can fail to give a complete answer is a refusal.
read_in_use() {
    local ids mounts via_current dir pid cwd comm stat state cwds="" unreadable=()
    readlink /proc/self/cwd >/dev/null 2>&1 \
        || refuse "there is no /proc to read process working directories from (not Linux), so a process could be inside any tree"
    ids="$("$DOCKER" ps -aq)" \
        || refuse "cannot list containers with '$DOCKER ps -aq'; their mounts decide what is in use"
    mounts=""
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086  # ids is a whitespace-separated list of hex container ids
        mounts="$("$DOCKER" inspect --format '{{range .Mounts}}mount{{"\t"}}{{$.Name}}{{"\t"}}{{.Source}}{{"\n"}}{{end}}' $ids)" \
            || refuse "cannot inspect the containers '$DOCKER ps -aq' listed; their mounts decide what is in use"
    fi
    via_current="$(printf '%s\n' "$mounts" | awk -F'\t' -v a="$ROOT/current" -v b="$REAL_ROOT/current" '
        NF == 3 && ($3 == a || $3 == b || index($3, a "/") == 1 || index($3, b "/") == 1) {
            n = $2; sub(/^\//, "", n); if (!(n in seen)) { seen[n] = 1; l = l (l == "" ? "" : ", ") n } }
        END { print l }')"
    [ -z "$via_current" ] || refuse "container(s) $via_current mount a path through $ROOT/current, so which release tree they hold cannot be proven; recreate them against a resolved $RELEASES/platform-v<N> path (or restart their stack) and re-run"
    for dir in /proc/[0-9]*; do
        pid="${dir#/proc/}"
        if cwd="$(readlink "$dir/cwd" 2>/dev/null)"; then
            comm="?"
            { read -r comm < "$dir/comm"; } 2>/dev/null || true
            cwds+="cwd"$'\t'"pid $pid ($comm)"$'\t'"${cwd% (deleted)}"$'\n'
        elif { read -r stat < "$dir/stat"; } 2>/dev/null; then
            # A zombie has no working directory; a process whose stat is gone has exited.
            state="${stat##*) }"
            case "${state%% *}" in Z|X) ;; *) unreadable+=("$pid") ;; esac
        fi
    done
    [ "${#unreadable[@]}" -eq 0 ] \
        || refuse "cannot read the working directory of pid(s) ${unreadable[*]}, which could be inside any tree; a full scan needs root, so on the hub VM run it with sudo"
    IN_USE="$mounts"$'\n'"$cwds"
}

# The reasons a release is in use according to IN_USE, one per line: "mounted by a,b" for
# containers and "cwd of pid N (comm)" for each process. A tree is matched by its literal path
# and by its resolved one, so a root reached through a symlink still matches a path recorded
# resolved.
users_of() {
    local tag="$1" literal real
    literal="$RELEASES/$tag"
    real="$(cd "$literal" && pwd -P)"
    printf '%s\n' "$IN_USE" | awk -F'\t' -v a="$literal" -v b="$real" '
        NF == 3 && ($3 == a || $3 == b || index($3, a "/") == 1 || index($3, b "/") == 1) {
            if ($1 == "mount") {
                n = $2; sub(/^\//, "", n)
                if (!(n in seen)) { seen[n] = 1; m = m (m == "" ? "" : ",") n }
            } else print "cwd of " $2 }
        END { if (m != "") print "mounted by " m }'
}

read_in_use

# Releases, newest first. Only platform-v<N> directories are candidates; anything else is
# reported and left alone.
tags=()
while IFS= read -r name; do
    [ -n "$name" ] || continue
    if [ -L "$RELEASES/$name" ] || [ ! -d "$RELEASES/$name" ] || [ -z "$(version_of "$name")" ]; then
        say "leave   $name (not a platform-v<N> release directory)"
        continue
    fi
    tags+=("$name")
done < <(ls -1A "$RELEASES")
[ "${#tags[@]}" -gt 0 ] || { say "no releases under $RELEASES"; exit 0; }
sorted=()
while IFS= read -r t; do sorted+=("$t"); done < <(
    for t in "${tags[@]}"; do printf '%s\t%s\n' "$(version_of "$t")" "$t"; done | sort -t$'\t' -k1,1nr | cut -f2)

current_v="$(version_of "$CURRENT")"
PREVIOUS=""
for t in "${sorted[@]}"; do
    if [ "$(version_of "$t")" -lt "$current_v" ]; then PREVIOUS="$t"; break; fi
done

say "root $ROOT, current $CURRENT, keep $KEEP newest, $([ "$APPLY" = 1 ] && echo apply || echo 'dry run')"
remove=()
rank=0
for t in "${sorted[@]}"; do
    rank=$((rank + 1))
    reasons=()
    [ "$t" = "$CURRENT" ] && reasons+=("current")
    [ "$t" = "$PREVIOUS" ] && reasons+=("previous")
    [ -n "$ASSEMBLING" ] && [ "$t" = "$ASSEMBLING" ] && reasons+=("assembling")
    [ "$rank" -le "$KEEP" ] && reasons+=("recent")
    while IFS= read -r r; do [ -n "$r" ] && reasons+=("$r"); done < <(users_of "$t")
    if [ "${#reasons[@]}" -gt 0 ]; then
        joined="$(IFS=';'; echo "${reasons[*]}")"
        say "keep    $t (${joined//;/, })"
    else
        say "remove  $t ($(du -sh "$RELEASES/$t" 2>/dev/null | cut -f1))"
        remove+=("$t")
    fi
done

if [ "${#remove[@]}" -eq 0 ]; then
    say "nothing to remove"
    exit 0
fi
if [ "$APPLY" != 1 ]; then
    say "dry run: ${#remove[@]} tree(s) would be removed; pass --apply to remove them"
    exit 0
fi

for t in "${remove[@]}"; do
    # The plan above may be seconds old; run every check again so a container or process that
    # appeared since still protects its tree.
    read_in_use
    users="$(users_of "$t" | paste -sd';' -)"
    if [ -n "$users" ]; then
        say "keep    $t (${users//;/, } since the plan was made)"
        continue
    fi
    rm -rf -- "${RELEASES:?}/$t"
    removed+=("$t")
    say "removed $t"
done
