#!/bin/sh
# Refuse a schema write when the connected instance is not this stack's own Postgres.
#
# The hazard is `make schema-apply` typed out of habit against a settings directory that names
# production. It exists today with no tunnel and no new option: a developer holding a valid
# LAMIN_API_KEY can run `make lamin-connect`, which writes settings naming the control-plane VM's
# public address, and the next apply reconciles this repository's declaration into production.
#
# A make parameter cannot carry this. The accident is the invocation with NO parameter, so a
# per-invocation flag is absent exactly when it is needed. Which instance is connected is
# PERSISTENT state instead: lamindb_setup keeps it in $LAMIN_HOME/current_instance.env, which both
# lamindb services bind at /root/.lamin, so that file is what this reads.
#
# IT IS AN ALLOWLIST, and that is deliberate. Production's address is not written down in this
# repository and changes when the VM's floating address does, so a guard that matched it by value
# would go stale silently and pass. What this repository CAN state is which database its own
# containers reach: the postgres container, in whose network namespace every schema verb runs.
# So a connection to that cluster is allowed and everything else is refused, which fails closed.
#
# THE VM IS UNCHANGED BY THIS. There, `make schema-apply` against production is the supported
# path, and it stays allowed: cloud-init's lamin-connect.sh applies LAMIN_DB_HOST_OVERRIDE=postgres
# (scripts/lamin_connect.sh), so the VM's own settings name the postgres container on port 5432,
# which is this allowlist. On a developer machine that override is empty, the settings keep
# LaminHub's public host, and the same file is refused.
#
# ONLY THE WRITE VERB TAKES THIS. Reading production is legitimate and is the point of
# make schema-review; plan, check and audit write nothing. schema-apply is the one that does.
#
# IT NEVER ECHOES THE CONNECTION STRING. current_instance.env holds the Postgres URI with its
# password, so the line is read into a variable and only the host, port and database name are ever
# printed. The password is dropped by the parse rather than masked afterwards.
#
# Usage: schema_write_guard.sh VERB
set -eu

verb=${1:-a schema write}

# The run's LAMIN_HOME, resolved the way the run resolves it. compose prefers the environment over
# --env-file and make exports a command-line variable, so `make schema-apply LAMIN_HOME=...` reaches
# the container from the environment and hub-database/.env is only consulted when it does not.
# Reading .env unconditionally would guard a directory the run does not use, which is a false pass.
#
# Either way the value goes through host_path.sh, because compose resolves a relative bind source
# against hub-database/ rather than against the caller's directory, and a guard that resolved it
# against make's cwd would stat nothing, fail open, and let the run reach whatever the container
# actually binds. That is the same false pass, arrived at from the other side.
if [ -n "${LAMIN_HOME:-}" ]; then
    home=$(sh "$(dirname "$0")/host_path.sh" LAMIN_HOME "$LAMIN_HOME")
else
    home=$(sh "$(dirname "$0")/host_path.sh" LAMIN_HOME)
fi

settings="$home/current_instance.env"

# No settings file means no connected instance, so there is nothing pointed anywhere. The verb then
# fails on its own exactly as it does today; refusing here would be this guard inventing a new
# failure for a case it was not written for.
[ -f "$settings" ] || exit 0

line=$(grep -E '^[[:space:]]*lamindb_instance_db=' "$settings" | tail -1) || line=''
dsn=${line#*=}

# "null" is what lamindb_setup writes for an instance with no database URL, which is a local sqlite
# one. It cannot be production and it is what every throwaway instance in tests/explorer uses.
case $dsn in
    ''|null|sqlite:*) exit 0 ;;
esac

# The LAST @ separates userinfo from the endpoint, so a percent-encoded credential holding one
# cannot move the split. Nothing below this line touches the part before it.
rest=${dsn#*://}
endpoint=${rest##*@}
hostport=${endpoint%%/*}
database=${endpoint#*/}
database=${database%%\?*}
case $endpoint in
    */*) ;;
    *) database='' ;;
esac

# A URL with no port means 5432, which is Postgres's own default and this stack's published one.
# An IPv6 literal keeps its brackets, because that is how it is spelled in a URL and the allowlist
# below is matched against that spelling rather than against a form invented here. It is also why
# the split is on the closing bracket first: the address itself is full of colons, so splitting on
# one would take [::1] apart and leave neither half matching anything.
case $hostport in
    '['*']')    host=$hostport; port=5432 ;;
    '['*']:'*)  host=${hostport%%]*}']'; port=${hostport##*]:} ;;
    *:*)        host=${hostport%:*}; port=${hostport##*:} ;;
    *)          host=$hostport; port=5432 ;;
esac

# The names this stack's own Postgres answers to. localhost because every schema verb runs in the
# postgres container's network namespace; postgres and database because that is the container name
# and the compose service name, which is what LAMIN_DB_HOST_OVERRIDE rewrites the host to on the VM.
case $host in
    localhost|127.0.0.1|'[::1]'|postgres|database) ;;
    *)
        reason="host $host is not this stack's Postgres" ;;
esac

# The port matters as much as the host. Under a forwarded database port the host is loopback and
# the endpoint is still remote, so a host-only check would pass the very shape it exists to refuse.
if [ -z "${reason:-}" ] && [ "$port" != "5432" ]; then
    reason="port $port is not this stack's Postgres port 5432"
fi

[ -n "${reason:-}" ] || exit 0

echo "schema_write_guard.sh: refusing $verb: $reason." >&2
echo "schema_write_guard.sh: the connected instance's database is ${host}:${port}/${database}," >&2
echo "schema_write_guard.sh: read from $settings. On a developer machine that is the production" >&2
echo "schema_write_guard.sh: instance, and an apply there would reconcile this declaration into" >&2
echo "schema_write_guard.sh: it. Reading it is fine: make schema-plan, schema-check, schema-audit" >&2
echo "schema_write_guard.sh: and schema-review are all still allowed." >&2
echo "schema_write_guard.sh: To apply locally, run make lamin-local first. To apply to production," >&2
echo "schema_write_guard.sh: run it on the control-plane VM. If this refused ON the VM, add" >&2
echo "schema_write_guard.sh: LAMIN_DB_HOST_OVERRIDE=postgres to hub-database/.env, or re-run" >&2
echo "schema_write_guard.sh: /usr/local/bin/lamin-connect.sh, which passes it itself." >&2
exit 1
