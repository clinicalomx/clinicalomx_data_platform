#!/bin/bash
# Canonical lamin connect: authenticate (LAMIN_API_KEY) + connect the hub instance.
# The single source of truth for "connect the instance". Run it INSIDE any container that
# has the lamin CLI on PATH:
#   - local dev: `make lamin-connect` execs it in the python-lamindb container
#                (workdir /workspace = hub-database/, so the path is scripts/lamin_connect.sh)
#   - the VM:    /usr/local/bin/lamin-connect.sh (cloud-init) runs it in the review image
#                and writes the host ~/.lamin that every service bind-mounts
# The instance lives on LaminHub as LAMIN_INSTANCE (lamin.instance in site.yaml, via site.env);
# its Postgres DB is the hub's.
# Supersedes the retired local-instance manager lamin_connect.py (clinicalomxdb-local).
set -euo pipefail
: "${LAMIN_INSTANCE:?set LAMIN_INSTANCE, lamin.instance in site.yaml; scripts/site/render.py writes it to site.env}"

lamin login                      # uses LAMIN_API_KEY from the environment
lamin connect "$LAMIN_INSTANCE"

# On the VM, Postgres is local. LaminHub registers the instance DB with a PUBLIC host (a
# floating/old IP) that on-VM containers can't reach: the old IP is dead, and Nectar
# floating IPs generally don't hairpin. So rewrite the db host in the just-written
# ~/.lamin/*.env to a local override (the postgres container on clinicalomxdb_net) so on-VM
# clients connect locally. LAMIN_DB_HOST_OVERRIDE comes from hub-database/.env, which is where the
# VM's cloud-init appends postgres and where a dev box leaves it EMPTY (keep whatever the hub
# registered), so this rewrite is right on both without an argument. Empty and unset behave the
# same here. Port + user + password + db are preserved.
if [ -n "${LAMIN_DB_HOST_OVERRIDE:-}" ]; then
  for f in "$HOME"/.lamin/*.env; do
    [ -f "$f" ] || continue
    sed -i -E "s#@[^@/:]+:([0-9]+)/#@${LAMIN_DB_HOST_OVERRIDE}:\1/#" "$f"
  done
fi
