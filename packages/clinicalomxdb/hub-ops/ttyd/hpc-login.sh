#!/usr/bin/env bash
# Interactive HPC login, run inside the portal's login terminal.
#
# Asks for an HPC username, then establishes a DUO-authenticated ControlMaster socket for that
# account. This is the ONE step in HPC submission that cannot be automated: a service account
# with key authentication was checked with the facility and is not possible.
#
# Why this takes the username as input rather than being pinned per user. The socket path
# template is ~/.ssh/cm-%r@%h:%p, and ssh expands %r to the remote user, so passing the account
# with -o User is enough to get a distinct socket per user with NO ssh_config entry and NO
# systemd unit per user. That is what lets the portal offer "add a connection" as a login rather
# than as an operator provisioning task.
#
# SECURITY. The command ttyd runs is fixed: this script and nothing else. The only client-
# supplied value is the username, which is validated against a strict allowlist and then passed
# as a single argv element to ssh, never through a shell, so it cannot become another argument
# or a command. Everything else about the connection is fixed here. ttyd's --url-arg is
# deliberately not enabled, so the client cannot supply arguments at all; the username arrives
# by being typed at the prompt below.
#
# The password and second factor are read by ssh from the terminal directly. Nothing in this
# script or this repository parses, logs, stores or forwards them.
#
# Establishing a socket does NOT make that user able to run flows. Submission also needs an ssh
# alias, a Prefect connection block, a work pool and a worker, which are provisioned by
# hub-pipelines/prefect/workers/hpc/bootstrap-users.sh from users.conf. The portal's
# connection table shows which half of that is in place.
set -uo pipefail

HOST="${HPC_LOGIN_HOST:?HPC_LOGIN_HOST is unset: ops-ttyd.service sets it from hpc.login_host in site.yaml}"
CONTROL_PATH="${HPC_CONTROL_PATH:-$HOME/.ssh/cm-%r@%h:%p}"
CONTROL_PERSIST="${HPC_CONTROL_PERSIST:-96h}"

printf 'HPC login\n'
printf -- '-----------\n'
printf 'Establishes a multiplexed SSH session that the Prefect workers reuse to submit jobs.\n'
printf 'It is kept for %s of idle time, so this is normally needed once per rebuild.\n' "$CONTROL_PERSIST"
printf 'Your password and second factor go straight to ssh; this host does not store them.\n\n'

read -r -p 'HPC username: ' user || exit 1

# Strict allowlist. The value is only ever passed as one argv element, so this is about turning
# a typo into a clear message rather than an obscure ssh failure.
if ! printf '%s' "${user:-}" | grep -Eq '^[a-z][a-z0-9._-]{0,31}$'; then
    printf '\nRejected: expected a lowercase account name such as alice.\n'
    printf 'Press enter to close.\n'
    read -r _ || true
    exit 1
fi

printf '\nConnecting to %s as %s ...\n\n' "$HOST" "$user"

# ControlMaster=auto creates the master if absent and reuses it if present, so re-running this
# for an already-authenticated user returns immediately without prompting.
if ssh \
    -e none \
    -o ControlMaster=auto \
    -o ControlPath="$CONTROL_PATH" \
    -o ControlPersist="$CONTROL_PERSIST" \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=3 \
    -o User="$user" \
    "$HOST" true
then
    printf '\nConnected. A ControlMaster socket now exists for %s.\n' "$user"
    printf 'The connection table will show it as alive within a few seconds.\n'
else
    status=$?
    printf '\nFailed (exit %s). Common causes: wrong password, the second factor timed out,\n' "$status"
    printf 'or this account has no access to %s.\n' "$HOST"
fi

printf '\nPress enter to close.\n'
read -r _ || true
