#!/bin/sh
# Print the host uid:gid that `make lamin-connect` hands the settings directory back to, or refuse.
#
# The container writes LAMIN_HOME as root, so the connect chmods and chowns the whole directory to
# leave credentials owned by the host user and readable only by them. That pair is RECURSIVE and
# unbounded, so what it is pointed at matters more than what it does. LAMIN_HOME=~ resolves to
# $HOME in compose and in host_path.sh alike, and stat on /home returns 0:0 on a stock Linux box,
# so that value would take an operator's entire home to 0700 owned by root, which the account
# cannot undo without sudo.
#
# The owner comes from the PARENT of LAMIN_HOME because the intent is to hand a project-scoped
# settings directory back to the human who owns the tree it sits in; the Makefile comment on
# lamin-connect carries the full account of why the invoker cannot be asked. This refuses every
# resolution where that reasoning does not hold:
#
#   the filesystem root         there is no parent to ask
#   the invoking user's home    a home directory is not a leaf this project may rewrite
#   a parent owned by root      nothing under it belongs to a host user to hand back to
#
# Refusing is deliberate rather than skipping the chown quietly: skipping leaves the credentials
# root-owned and world-readable, which is the hazard the chmod exists to close.
#
# Usage: lamin_home_owner.sh
set -eu

dir=$(sh "$(dirname "$0")/host_path.sh" LAMIN_HOME)

refuse() {
    echo "lamin_home_owner.sh: refusing LAMIN_HOME=$dir: $1" >&2
    echo "lamin_home_owner.sh: lamin-connect rewrites that path recursively, so LAMIN_HOME in" >&2
    echo "lamin_home_owner.sh: hub-database/.env must name a directory this project owns, such as" >&2
    echo "lamin_home_owner.sh: ~/.clinicalomxdb/lamin, and not a home directory or a system path." >&2
    exit 1
}

[ -d "$dir" ] || refuse "it is not a directory; create it with make db-dirs"

real=$(cd "$dir" && pwd -P)
parent=$(cd "$dir/.." && pwd -P)

[ "$real" != "$parent" ] || refuse "it is the filesystem root"

if home=$(cd "${HOME:-/nonexistent}" 2>/dev/null && pwd -P); then
    [ "$real" != "$home" ] || refuse "it is your home directory $home"
fi

owner=$(stat -c '%u:%g' "$parent" 2>/dev/null) \
  || refuse "the owner of its parent $parent cannot be read"

case $owner in
    0:*) refuse "its parent $parent is owned by root, so there is no host user to hand it back to" ;;
esac

printf '%s\n' "$owner"
