# Reload one container's bind-mounted config in place. Three outcomes, deliberately distinct:
# not running is fine and says so, a successful reload says so, and a REJECTED reload is an error
# that stops the target rather than leaving the old config running behind a cheerful message.
# $(1) container, $(2) label, $(3) command to run inside it.
# Refuse to reload a config the container cannot actually see.
#
# A single-file bind mount pins the file's INODE. git replaces a file by writing a new one and
# renaming it over the old, so the container keeps reading the original forever while the host
# shows the new one -- and a reload then re-reads the stale file and reports success. Every
# config here is now a DIRECTORY mount, which does not have that problem, but this check is what
# turns a recurrence into one line of output instead of an afternoon.
# $(1) container, $(2) label, $(3) path inside the container, $(4) path on the host.
define assert_config_visible
if docker ps --format '{{.Names}}' | grep -qx '$(1)'; then \
    inside=$$(docker exec $(1) md5sum '$(3)' 2>/dev/null | cut -d' ' -f1); \
    outside=$$(md5sum '$(4)' 2>/dev/null | cut -d' ' -f1); \
    if [ -n "$$inside" ] && [ -n "$$outside" ] && [ "$$inside" != "$$outside" ]; then \
        echo "ERROR: $(2) is reading a DIFFERENT $(3) than the one on disk"; \
        echo "    the container cannot see your edit, so reloading would silently do nothing"; \
        echo "    recreate it: $(OPS_COMPOSE) up -d --force-recreate $(2)"; \
        exit 1; \
    fi; \
fi
endef

define reload_or_fail
if ! docker ps --format '{{.Names}}' | grep -qx '$(1)'; then \
    echo "$(2) not running, nothing to reload"; \
else \
    out=$$(docker exec $(1) $(3) 2>&1) \
      && echo "$(2) config reloaded" \
      || { echo "ERROR: $(2) reload FAILED; it is still running the OLD config"; \
           echo "$$out" | sed 's/^/    /'; \
           hint='$(strip $(4))'; [ -n "$$hint" ] && echo "    HINT: $$hint"; \
           exit 1; }; \
fi
endef

# Reload by SIGNAL rather than over HTTP, for a component whose image ships no HTTP client at all.
# The signal itself always succeeds, so success is not evidence of anything; the component's own
# log is the only report of whether the new config was accepted. $(1) container, $(2) label,
# $(3) the error string it logs on rejection, $(4) hint.
#
# A container younger than RELOAD_MIN_AGE is NOT signalled. ops-up runs compose up -d and then
# ops-reload, so on any deploy that recreates the container the HUP lands a second or two into
# startup, before the process has installed its own handler; it then dies on the default
# disposition (exit 2, no log at all) and docker kill has already cancelled the restart manager,
# so restart: unless-stopped never brings it back. A container that young started from the config
# now on disk, so the reload is pointless as well as fatal. If the age cannot be computed, signal
# as before rather than skipping silently.
#
# The age is read with python3 rather than `date -d`, which is GNU-only: an operator's laptop is
# a BSD date and would silently fall through to signalling. python3 already gates ops-up through
# ops-env-check, so this adds no dependency.
RELOAD_MIN_AGE = 15
CONTAINER_AGE  = python3 -c 'import sys,datetime as dt; started=dt.datetime.fromisoformat(sys.stdin.read().strip().replace("Z","+00:00")); print(int((dt.datetime.now(dt.timezone.utc) - started).total_seconds()))'
define reload_signal_or_fail
if ! docker ps --format '{{.Names}}' | grep -qx '$(1)'; then \
    echo "$(2) not running, nothing to reload"; \
else \
    age=$$(docker inspect -f '{{.State.StartedAt}}' '$(1)' 2>/dev/null | $(CONTAINER_AGE) 2>/dev/null); \
    if [ -n "$$age" ] && [ "$$age" -lt $(RELOAD_MIN_AGE) ]; then \
        echo "$(2) started $${age}s ago, already on the current config; not signalling"; \
    else \
        docker kill -s HUP '$(1)' >/dev/null \
          || { echo "ERROR: $(2) could not be signalled"; exit 1; }; \
        sleep 3; \
        bad=$$(docker logs --since 10s '$(1)' 2>&1 | grep '$(3)'); \
        if [ -n "$$bad" ]; then \
            echo "ERROR: $(2) reload FAILED; it is still running the OLD config"; \
            echo "$$bad" | sed 's/^/    /'; \
            hint='$(strip $(4))'; [ -n "$$hint" ] && echo "    HINT: $$hint"; \
            exit 1; \
        fi; \
        echo "$(2) config reloaded"; \
    fi; \
fi
endef


DB_COMPOSE       = docker compose -f hub-database/docker-compose.yml --env-file hub-database/.env
PIPE_COMPOSE     = docker compose -f hub-pipelines/docker-compose.yml --env-file hub-pipelines/.env
OPS_COMPOSE      = docker compose -f hub-ops/docker-compose.yml --env-file hub-ops/.env
LAMINDB_EXEC     = $(DB_COMPOSE) exec
LAMINDB_SVC      = $(LAMINDB_EXEC) python-lamindb
LAMIN_CONNECT    = scripts/lamin_connect.sh
LAMIN_LOCAL      = scripts/lamin_local.py

# Site values: the repository root's site.env, which scripts/site/render.py writes from site.yaml.
# Exported so compose interpolation and the recipes see them. Absent, LAMIN_INSTANCE is empty and
# every compose file naming a site value refuses to start, each naming the value it lacks.
-include ../../site.env
export LAMIN_INSTANCE OPS_PORTAL_URL CLINICALOMX_SITE_IMAGE_REGISTRY \
       CLINICALOMX_SITE_RAW_ROOT CLINICALOMX_SITE_RAW_SCRATCH_ROOT \
       CLINICALOMX_SITE_HPC_ROOT CLINICALOMX_SITE_HPC_NAT_ADDRESS \
       CLINICALOMX_SITE_HPC_COMPUTE_SUBNET

NETWORK          = clinicalomxdb_net

.PHONY: network \
        db-up db-down db-nuke db-status db-shell \
        lamin-connect lamin-local lamin-close lamin-status lamin-home-check \
        db-build schema-plan schema-apply schema-check schema-audit schema-version \
        schema-review schema-write-check \
        pipelines-up pipelines-down pipelines-nuke pipelines-status \
        ops-up ops-down ops-status ops-logs ops-health ops-health-status \
        ops-schema-review ops-schema-review-status \
        hpc-users ops-ssh ops-login hpc-users-status tunnel tunnel-close ops-dirs ops-dashboards \
        ops-alert-rules \
        db-env-check pipelines-env-check ops-env-check db-dirs db-certs \
        ui-dev ui-shots ui-notes ui-fixtures-check ops-reload ops-caddy-recreate explorer-test \
        all-up all-down \
        tunnel-model tunnel-model-close \
        fresh-start reconnect help

help:
	@echo "clinicalOMx DB - multi-stack service management"
	@echo ""
	@echo "Network (run once before first up):"
	@echo "  make network          Create the shared clinicalomxdb_net Docker network"
	@echo ""
	@echo "Database hub (hub-database/):"
	@echo "  make db-up            Start database services"
	@echo "  make db-down          Stop containers; the instance stays connected"
	@echo "  make db-nuke          Remove containers and the pgadmin volume. KEEPS the Postgres"
	@echo "                        cluster at DB_DATA_DIR and the settings directory at"
	@echo "                        LAMIN_HOME, and prints how to remove each."
	@echo "  make db-status        Show database service status + lamin info"
	@echo "  make db-shell         Open a bash shell inside the lamindb container"
	@echo "  make db-env-check     Fail if hub-database/.env drifted from environment.example"
	@echo ""
	@echo "LaminDB instance (the hub one is $(LAMIN_INSTANCE); lamin-local makes a local one):"
	@echo "  make lamin-connect    Authenticate + connect the hub instance (writes ~/.lamin)"
	@echo "                        Its database is the VM's, registered at a public address"
	@echo "                        that does not route from a developer machine, so the"
	@echo "                        schema-* verbs then fail at connect time. Use lamin-local"
	@echo "                        below when you have no route to the VM."
	@echo "  make lamin-local      Create + connect a LOCAL instance on this stack's Postgres"
	@echo "                        No LaminHub account and no credential of any kind. Its own"
	@echo "                        database and a storage root under LAMIN_HOME, so it cannot"
	@echo "                        touch production. The FIRST build does need a route to"
	@echo "                        lamindb's hub for one unauthenticated storage query that"
	@echo "                        2.8.1 cannot skip; every run after it is fully offline."
	@echo "                        Idempotent: run it again to reconnect."
	@echo "                        Then schema-apply and schema-review have something local to"
	@echo "                        run against (docs/runbooks/schema-review.md). NOT for the"
	@echo "                        VM: it would point the Explore tab, the review app and the"
	@echo "                        schema-* verbs away from the production instance."
	@echo "  make lamin-close      Disconnect from the current instance"
	@echo "                        Not local: it clears the shared host ~/.lamin, so the"
	@echo "                        Explore tab and the schema-* verbs lose it too. On the VM"
	@echo "                        lamin-connect puts it back only if .env names"
	@echo "                        LAMIN_DB_HOST_OVERRIDE=postgres, which cloud-init appends."
	@echo "  make lamin-status     Show the current lamin user + instance"
	@echo "  make lamin-home-check Fail if LAMIN_HOME names a path lamin-connect must not"
	@echo "                        rewrite recursively: a home directory, the filesystem"
	@echo "                        root, or anything whose parent is owned by root."
	@echo ""
	@echo "Vocabulary (the clinicalomx package, installed in the image):"
	@echo "  make db-build         Rebuild the lamindb image from this checkout's"
	@echo "                        clinicalomx, markers, ledger and reagents wheels."
	@echo "                        Needs uv on PATH; no credential."
	@echo "  make schema-plan      Show what apply would change (no writes)"
	@echo "  make schema-apply     Converge the instance toward the declared state"
	@echo "                        REFUSES when the connected instance's database is not this"
	@echo "                        stack's own Postgres, which off the VM means production."
	@echo "                        The read verbs are unaffected. On the VM the connection is"
	@echo "                        local, so an apply there is allowed as before."
	@echo "  make schema-check     Exit non-zero on drift (CI gate)"
	@echo "  make schema-audit     What the foundation misses, and who owns the rest"
	@echo "  make schema-version   Show the clinicalomx version in the image"
	@echo "                        Each of these starts a NEW container, which reads the"
	@echo "                        connected instance from the ~/.lamin that LAMIN_HOME"
	@echo "                        names in hub-database/.env. Connect first: lamin-local"
	@echo "                        off the VM, lamin-connect on it, where cloud-init has"
	@echo "                        already written it."
	@echo "                        plan/apply cover the FOUNDATION only. Add"
	@echo "                        WITH_CONSUMERS=1 to also reconcile every consumer"
	@echo "                        vocabulary installed in the image (clinicalomx_markers)."
	@echo "                        A consumer not in Dockerfile.lamindb is invisible, not"
	@echo "                        missing; schema-audit names an owner per feature."
	@echo "  make schema-review    Render the apply as a page to read and annotate"
	@echo "                        Writes hub-database/$(SCHEMA_REVIEW_OUT); open it with"
	@echo "                        lavish-axi. Panel 1 is GREEN when every declared name was"
	@echo "                        CONFIRMED to land with the declared dtype, and FAILING when"
	@echo "                        one did not land or landed under another dtype, which is the"
	@echo "                        failure a passing test suite cannot see. Panel 2 is GREEN"
	@echo "                        when every sampled feature value was judged by lamin_api's"
	@echo "                        own validators and none was refused, and FAILING naming each"
	@echo "                        cell that was. Either panel is AMBER when it could not"
	@echo "                        confirm or could not judge, with the count, and panel 1 is"
	@echo "                        AMBER for a deprecated declaration that deliberately did not"
	@echo "                        land, so green never stands for nothing-to-report. A name"
	@echo "                        nobody declared is reported and never fails: the declaration"
	@echo "                        is a floor (ADR 0003). Panel 3 is the verdict, which is"
	@echo "                        yours. Read-only: correct the declaration, the seed row or"
	@echo "                        the curation rule, then schema-apply and regenerate. Exits 0"
	@echo "                        even on drift; schema-check is the gate. It compares"
	@echo "                        against the FOUNDATION data files and ignores"
	@echo "                        WITH_CONSUMERS. Needs a connected instance, same as the"
	@echo "                        schema-* verbs above; it refuses rather than reporting"
	@echo "                        every declared name as one that did not land."
	@echo ""
	@echo "Pipelines hub (hub-pipelines/):"
	@echo "  make pipelines-up     Start MLflow and Prefect"
	@echo "  make pipelines-down   Stop pipeline services"
	@echo "  make pipelines-nuke   Remove containers and volumes"
	@echo "  make pipelines-status Show pipeline service status"
	@echo "  make pipelines-env-check Fail if hub-pipelines/.env drifted from environment.example"
	@echo ""
	@echo "Operations surface (hub-ops/), run ON THE VM:"
	@echo "  make ops-up           Start the portal, logging and metrics stack"
	@echo "  make ops-down         Stop the operations surface"
	@echo "  make ops-status       Show operations service status"
	@echo "  make ops-reload       Re-read every bind-mounted config; fails if one is rejected"
	@echo "  make ops-caddy-recreate Recreate the proxy; only for a global-options change (DROPS conns)"
	@echo "  make ops-dashboards   Regenerate the provisioned Grafana dashboards"
	@echo "  make ops-alert-rules  Render the Grafana alert rules from site.env; ops-up and"
	@echo "                        ops-reload run it first"
	@echo "  make explorer-test    Run the tests/explorer suite: the Explore tab against a"
	@echo "                        throwaway instance, plus the schema review and the"
	@echo "                        LAMIN_HOME guard, which need no instance at all"
	@echo "  make ops-logs         Tail operations service logs"
	@echo "  make ops-health       Install/restart the health endpoint (host systemd unit)"
	@echo "  make ops-health-status Show the health unit + current JSON"
	@echo "  make ops-schema-review Install/restart the schema review renderer (host systemd unit)"
	@echo "                        It serves the last page it rendered at /schema-review/ on the"
	@echo "                        portal and re-renders only when the button on that tab is"
	@echo "                        pressed. Reading production needs no forward and no credential"
	@echo "                        off this box; docs/runbooks/schema-review.md is the procedure."
	@echo "  make ops-schema-review-status Show that unit + when its page was rendered"
	@echo "  make ops-env-check    Fail if hub-ops/.env drifted from environment.example"
	@echo ""
	@echo "HPC socket broker (users.conf), run ON THE VM in this order:"
	@echo "  make hpc-users      ssh aliases + Prefect blocks/pools + per-user workers"
	@echo "  make ops-login        HPC login relay for the portal widgets (/login/)"
	@echo "  make ops-ssh          HPC login terminal at /ssh/ (fallback path)"
	@echo "  make hpc-users-status Show per-user workers and ControlMaster sockets"
	@echo ""
	@echo "Portal UI development (local only, never on the VM):"
	@echo "  make ui-dev           Live preview with fixture data at 127.0.0.1:$(UI_DEV_PORT)"
	@echo "  make ui-shots         Screenshot every UI state to hub-ops/devserver/shots/"
	@echo "  make ui-notes         Show interactive edits queued from the inspector"
	@echo "  make ui-fixtures-check Fail if fixtures drifted from the real /health shape"
	@echo ""
	@echo "Operator tunnel, run on YOUR machine (not the VM):"
	@echo "  make tunnel           Open the operator tunnel to the portal"
	@echo "  make tunnel-close     Tear the tunnel down"
	@echo "  make tunnel-model     Expose a locally-served model to the VM (MODEL_PORT=8000)"
	@echo "  make tunnel-model-close Tear the model tunnel down"
	@echo ""
	@echo "Composite:"
	@echo "  make all-up           Start all hubs"
	@echo "  make all-down         Stop all hubs"
	@echo "  make fresh-start      network + db-up + lamin-connect"
	@echo "                        The Explore tab does NOT pick the connection up on its own:"
	@echo "                        db-up starts it before the connect and lamindb resolves the"
	@echo "                        instance once at import, so run"
	@echo "                        docker restart clinicalomxdb_explorer afterwards."
	@echo "  make reconnect        lamin-close + lamin-connect"

network:
	docker network create $(NETWORK) 2>/dev/null || echo "Network $(NETWORK) already exists"

# The three per-host paths hub-database/.env names, created before compose binds them. Same
# reason as ops-dirs below: a bind source that does not exist stops the container, and these
# refuse to be conjured (create_host_path false in docker-compose.yml), so an absent or mistyped
# path is a refusal to start rather than an empty directory. Read from .env rather than repeated
# here, so there is one place a host path is written down. No sudo: two dev defaults are repo-local
# and LAMIN_HOME's is a project-scoped directory under the developer's home, and on the VM all three exist
# already.
#
# The value is read by hub-database/scripts/host_path.sh rather than by grep and cut here,
# because compose parses .env as dotenv and then resolves the bind source, and a shell reading
# the raw line disagrees with it over quoting, inline comments and a leading tilde. That script
# is the one place those rules are written down, and db-certs and db-nuke read through it too.
db-dirs: db-env-check
	@for key in DB_DATA_DIR LAMIN_HOME DB_CERT_DIR; do \
	    dir=$$(sh hub-database/scripts/host_path.sh "$$key") || exit 1; \
	    mkdir -p "$$dir" \
	      || { echo "ERROR: cannot create $$key=$$dir. On the VM this means the data disk is not mounted; do not create it by hand until it is."; exit 1; }; \
	done

# The TLS pair Postgres serves with, generated only when it is absent. The VM has its own from
# cloud_init.yaml.tftpl and this finds it already there, so the two never both write.
#
# Generated INSIDE a container for one reason: Postgres refuses a key file that is not owned by
# root or by its own user, and a developer cannot chown to uid 999 without sudo. The container is
# root, so it can, which is the same chown 999:999 and chmod 600 cloud-init does. The image is
# the one this stack already pulls, so this adds no dependency.
#
# A path that exists but is not a regular file is the leftover of the earlier configuration,
# where Docker created a DIRECTORY for each missing certificate. Say so rather than letting
# openssl fail on it.
db-certs: db-dirs
	@abs=$$(sh hub-database/scripts/host_path.sh DB_CERT_DIR) || exit 1; \
	for f in server.crt server.key; do \
	    if [ -e "$$abs/$$f" ] && [ ! -f "$$abs/$$f" ]; then \
	        echo "ERROR: $$abs/$$f exists and is not a file. Docker created it as a directory under the"; \
	        echo "       earlier configuration; remove it (it is root-owned, so: sudo rm -rf $$abs/$$f) and re-run."; \
	        exit 1; \
	    fi; \
	done; \
	if [ -f "$$abs/server.crt" ] && [ -f "$$abs/server.key" ]; then exit 0; fi; \
	echo "generating a self-signed Postgres certificate in $$abs"; \
	docker run --rm -v "$$abs:/certs" postgres:18 sh -c \
	  'openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
	     -keyout /certs/server.key -out /certs/server.crt -subj "/CN=clinicalomxdb_postgres" \
	   && chown 999:999 /certs/server.key /certs/server.crt \
	   && chmod 600 /certs/server.key'

db-up: db-env-check db-dirs db-certs
	$(DB_COMPOSE) up -d --remove-orphans
	@bash hub-database/scripts/wait_for_postgres.sh

# Stops the stack and LEAVES THE INSTANCE CONNECTED. It used to disconnect first, which was
# harmless while the connection lived in the python-lamindb container's own writable layer and
# died with it. Now that ~/.lamin is a host bind shared with the explorer and the schema-* verbs,
# disconnecting here would destroy state the stack does not own, and on the VM it would destroy
# the settings cloud-init corrected with LAMIN_DB_HOST_OVERRIDE, which `make lamin-connect`
# cannot write back. Stopping a stack is not a statement about which instance is connected; use
# make lamin-close when that is what you mean.
db-down:
	$(DB_COMPOSE) stop

# down -v removes the containers and this stack's named volumes, which since the cluster became a
# plain bind means pgadmin_data and nothing else. DB_DATA_DIR survives, and that is deliberate:
# on the VM it is the mounted data disk, so no make target may become a one-command destroy of
# the real cluster. What this target owes you instead is an accurate account of what is left,
# because the trap is a fresh-start that silently comes up on the old cluster.
#
# The settings directory is the second survivor, and it is new. While the connection lived in the
# python-lamindb container's own writable layer, down -v took it; now LAMIN_HOME is a host bind,
# so the connection and the credentials in it outlive the containers. The account names it for
# two reasons: it still says which instance is connected, so a schema-* verb run against an
# emptied cluster gets a database error rather than the guard's refusal, and it still holds a real
# API key and a real database password that a developer who assumed the wipe took everything is
# now keeping without knowing it.
db-nuke:
	$(DB_COMPOSE) down -v
	@dir=$$(sh hub-database/scripts/host_path.sh DB_DATA_DIR 2>/dev/null || echo "your DB_DATA_DIR"); \
	lamin=$$(sh hub-database/scripts/host_path.sh LAMIN_HOME 2>/dev/null || echo "your LAMIN_HOME"); \
	echo "Removed: the containers and the pgadmin_data volume."; \
	echo "KEPT:    the Postgres cluster at $$dir."; \
	echo "KEPT:    the lamindb settings directory at $$lamin."; \
	echo ""; \
	echo "'make fresh-start' therefore comes up on that same cluster, with the password it was"; \
	echo "initialised with rather than whatever POSTGRES_PASSWORD now says. To start from an"; \
	echo "empty one, remove it deliberately first; it is owned by uid 999, so that needs docker:"; \
	echo "  docker run --rm -v $$dir:/cluster postgres:18 find /cluster -mindepth 1 -delete"; \
	echo ""; \
	echo "The settings directory holds LAMIN_API_KEY and the instance settings carrying the"; \
	echo "Postgres URI with its password, and it still names the connected instance. If you do"; \
	echo "empty the cluster, that name outlives the data it pointed at, so schema-review does not"; \
	echo "refuse; it fails querying tables the new cluster has not got. down -v already took the"; \
	echo "container, so there is no connection left to close and 'make lamin-close' would only"; \
	echo "report the service is not running; what survives is the files. Empty them and KEEP THE"; \
	echo "DIRECTORY: both lamindb binds refuse to create a missing host path, and the VM's boot"; \
	echo "unit calls compose directly rather than through db-dirs, so a removed directory leaves"; \
	echo "the whole stack down at its first ExecStart. The directory is owner-owned whichever"; \
	echo "connect wrote it, so unlike the cluster this needs no docker and no sudo:"; \
	echo "  find $$lamin -mindepth 1 -delete"

db-status:
	$(DB_COMPOSE) ps
	@echo ""
	$(LAMINDB_SVC) lamin info

db-shell:
	$(LAMINDB_SVC) bash

# Connect the hub instance via the canonical script (single source of truth, shared with the
# VM's cloud-init). LAMIN_API_KEY comes from hub-database/.env; instance from LAMIN_INSTANCE.
#
# It writes into the container's /root/.lamin, which is the host LAMIN_HOME bind, so what it
# connects is what every other lamindb container here reads. That is the point: a connect that
# only the container making it could see is what made `make schema-review` unreachable locally.
#
# Sharing that directory is also why LAMIN_DB_HOST_OVERRIDE has to reach this script rather than
# only cloud-init's copy of the same call. What `lamin connect` writes names the public host
# LaminHub registered, which on the VM is dead, so a connect that skipped the rewrite would point
# every reader of the shared ~/.lamin at an unreachable database. It arrives from
# hub-database/.env: the service declares env_file, and `compose exec` runs in the environment the
# container was created with, so there is no -e here and nothing for an operator to remember. The
# VM's .env carries LAMIN_DB_HOST_OVERRIDE=postgres, appended by cloud-init; a dev box leaves it
# empty, which the script reads as "keep whatever the hub registered".
#
# `make lamin-connect LAMIN_DB_HOST_OVERRIDE=postgres` IS NOT A WAY TO SUPPLY IT. make exports a
# command-line variable into this recipe's environment, but `compose exec` forwards only its own -e
# flags and the environment the container was created with, so the variable never reaches
# scripts/lamin_connect.sh: the rewrite is skipped and the connect writes the dead host with a zero
# exit. Two things do work. Append LAMIN_DB_HOST_OVERRIDE=postgres to hub-database/.env and
# re-create the container so env_file delivers it, which `make db-up` does. Or run
# /usr/local/bin/lamin-connect.sh on the VM, which passes the override itself.
#
# db-env-check is the guard on half of that, and it is worth knowing which half. hub-ops/env-check.py
# compares KEYS, so what it catches is the key being ABSENT, which is the shape of a .env predating
# cloud-init's append; a database-stack target then fails naming it, before this connect can write
# anything. What it does NOT catch is the key being present and EMPTY, because empty is the correct
# dev-box value and a check that rejected it would be wrong. On a VM an empty value passes the check
# and this connect exits zero having written the dead public host into the shared ~/.lamin, silently
# pointing the Explore tab, the review app and the schema-* verbs at a database none of them can
# reach. hub-database/environment.example says on its own line that postgres is the VM value, which
# is the only defence there is; a correctly provisioned VM does not depend on it, because cloud-init
# appends the value after the Barbican payload and a later assignment wins in an --env-file.
# fresh-start reaches the check through db-up, which runs before the connect, so it does not name
# it again. reconnect DOES name it itself, and that is not redundancy to be tidied away: make runs
# prerequisites left to right, so a reconnect reaching the guard only through this target would run
# lamin-close first and destroy the shared connection before refusing. The comment on reconnect
# itself carries the full account.
#
# The chown is what /usr/local/bin/lamin-connect.sh already does with /home/ubuntu/.lamin. The
# container writes the shared directory as root, and the directory is host state that host
# processes read, so it must not be left root-owned; only the container can hand it back, since
# the files are root-owned the instant they are written.
#
# The uid comes from the PARENT of LAMIN_HOME and never from whoever invoked make. Asking the
# invoker cannot be made reliable: `sudo make` answers through SUDO_UID, but `sudo su -` and `su -`
# reset the environment and drop it, so `id -u` is 0 and the pair resolves to chown 0:0 beside the
# 0700 below, which locks out the host readers of the shared directory. On the VM those are the
# health tile and the vm-process Prefect worker, both running as ubuntu, so the portal would report
# no instance on a box whose instance is connected. The parent answers the same in every invocation
# shape: /home/ubuntu on the VM, the developer's own home on a dev box.
#
# BOTH ARE RECURSIVE, so the value they are pointed at is the dangerous part rather than the
# operation. LAMIN_HOME=~ resolves to $HOME in compose and in host_path.sh alike, and /home stats
# as 0:0 on a stock Linux box, so that value alone would take a whole home directory to 0700 owned
# by root. hub-database/scripts/lamin_home_owner.sh is the one place that resolves the path and
# derives the uid, and it refuses a home directory, the filesystem root and a root-owned parent by
# name. Refusing loudly is the point: skipping the chown quietly would leave the credentials
# root-owned and world-readable instead. tests/explorer/test_lamin_home_guard.py holds it to that.
#
# lamin-home-check is a PREREQUISITE rather than a line in the recipe below, for the same reason
# db-env-check is one: make runs prerequisites left to right, so a refusal reached only from inside
# this recipe would let reconnect run lamin-close first and destroy the shared connection before
# refusing. A guard that fires after the damage is not a guard. Any composite that closes before
# connecting therefore has to reach it through lamin-connect's own prerequisite list.
#
# The chmod is beside it because it is the same problem: what lands here is credentials, meaning
# LAMIN_API_KEY in user_settings.env and instance settings holding the Postgres URI with its
# password. root writes them under the container's default umask, so they arrive 0644 and stay
# world-readable on the host. go-rwx takes the directory to 0700 and its contents to 0600. It runs
# before the chown, in the same exec, so there is no window in which the files are owned by the
# developer and still readable by everyone.
#
# THE TIGHTENING RUNS WHETHER THE CONNECT SUCCEEDED OR FAILED, which is why the recipe is one shell
# line holding the connect's status rather than three make lines. scripts/lamin_connect.sh is
# `set -euo pipefail` and runs `lamin login` BEFORE `lamin connect`, and login is what writes
# user_settings.env holding LAMIN_API_KEY. An expired key or an unreachable LaminHub therefore
# aborts the script with that file already on disk, and make abandoning the recipe there would
# leave it 0644 and root-owned in the directory the explorer, the review app and every schema-*
# verb bind, where the host user cannot even chmod it back without sudo. A login that wrote the key
# and then failed is exactly when it must not be left readable. The connect's exit status is kept
# in rc and re-raised afterwards, so the target still fails and the operator still sees the connect
# error rather than a green run.
lamin-home-check:
	@sh hub-database/scripts/lamin_home_owner.sh >/dev/null

lamin-connect: db-env-check lamin-home-check
	@owner=$$(sh hub-database/scripts/lamin_home_owner.sh) || exit 1; \
	$(LAMINDB_EXEC) -e LAMIN_INSTANCE=$(LAMIN_INSTANCE) python-lamindb bash $(LAMIN_CONNECT); \
	rc=$$?; \
	$(LAMINDB_SVC) sh -c "chmod -R go-rwx /root/.lamin && chown -R $$owner /root/.lamin" || exit $$?; \
	exit $$rc

# Create and connect a LOCAL instance instead, backed by this stack's own Postgres.
# hub-database/scripts/lamin_local.py argues the whole design; docs/runbooks/schema-review.md is
# the operator procedure.
#
# A SEPARATE VERB rather than a flag on lamin-connect, because the two do genuinely different
# things and the difference is which database ends up connected. lamin-connect connects the hub
# instance, whose database LaminHub registers at the VM's public address; from a developer
# machine that address does not route, so make schema-review fails at connect time. This target
# is for the developer who has no way to reach it and still needs a page to look at.
#
# It needs NO CREDENTIAL, which is the requirement that shaped it: no LAMIN_API_KEY, no LaminHub
# account, no index credential. It does need two things it cannot produce. The image, which a developer who
# cannot build it can still have from a colleague or from a previous build. And, on the FIRST build
# only, a route to lamindb's hub: init_storage issues one unauthenticated query for the storage
# location that lamindb 2.8.1 offers no way to skip, so an offline box fails there with a name
# resolution error. No credential is involved in it and nothing is registered by it, and every run
# after the first is fully offline. lamin_local.py's docstring carries the detail.
#
# ON THE VM THIS IS NOT WHAT YOU WANT, for the same reason lamin-close is not local there. It
# writes current_instance.env in the shared host ~/.lamin, so the Explore tab, the review app and
# every schema-* verb would follow it onto the local review instance and off the production one.
# It also writes an anonymous current_user.env there when that directory holds none, which is what
# keeps the schema-* verbs off a LaminHub login they have no key for; a current_user.env already
# there is a real login and is left untouched.
# It cannot damage production: the database it creates is its own and the storage root is under
# LAMIN_HOME. Put the box back with make lamin-connect, or /usr/local/bin/lamin-connect.sh.
#
# Same recipe shape and the same trailing chmod and chown as lamin-connect above, whose comment
# block carries the full account of why they run whether the command succeeded or failed and why
# the uid comes from the parent of LAMIN_HOME. Duplicated rather than factored into a define:
# there are two callers of one line, and a define capturing $$owner from the surrounding recipe
# shell would be less legible than the line it replaced.
lamin-local: db-env-check lamin-home-check
	@owner=$$(sh hub-database/scripts/lamin_home_owner.sh) || exit 1; \
	$(LAMINDB_SVC) python $(LAMIN_LOCAL); \
	rc=$$?; \
	$(LAMINDB_SVC) sh -c "chmod -R go-rwx /root/.lamin && chown -R $$owner /root/.lamin" || exit $$?; \
	exit $$rc

# ON THE VM THIS IS NOT LOCAL. It removes current_instance.env from the host ~/.lamin, so the
# Explore tab and the review app lose the instance too. `make lamin-connect` puts it back on a VM
# whose .env carries LAMIN_DB_HOST_OVERRIDE=postgres, which cloud-init appends. On a box
# provisioned before that append existed the key is absent, and without the override a connect
# writes settings naming a public host that is dead; there, add the key or re-run
# /usr/local/bin/lamin-connect.sh, which passes it itself.
lamin-close:
	$(LAMINDB_SVC) lamin disconnect

lamin-status:
	$(LAMINDB_SVC) lamin info

# --- Vocabulary -------------------------------------------------------------
# The vocabulary lives in the clinicalomx package, installed into the python-lamindb
# image (see Dockerfile.lamindb). This repo operates the instance; it does not carry the
# declaration. Rebuild with `make db-build`, which builds the wheels from this checkout.
#
# Adopting an instance (dump_instance.py) and running the package's tests are
# package-DEVELOPMENT operations, so they belong in a clinicalomx checkout, not here.
#
# DEFAULT_STORAGE names a key in the package's storages.yaml; leave it unset to keep the
# instance default, which is the right choice from a host that is not the compute node.
#
# These use `compose run --rm`, not `exec`, so each one is a NEW container. Which instance is
# connected is per-container state (lamindb_setup keeps it in $HOME/.lamin/current_instance.env),
# so a fresh container reads whatever /root/.lamin it is given and nothing else. The
# python-lamindb service therefore binds LAMIN_HOME there, exactly as the explorer service does;
# docker-compose.yml argues it at the mount. That is what makes the runbook's local pair work:
# `make lamin-connect` execs the long-lived container and `make schema-review` starts a new one,
# and both now write and read the same host directory.
#
# It is also how these verbs reach the database on the control-plane VM. LaminHub registers this
# instance's DB at a public host that is DEAD, so the settings a container writes for itself
# point somewhere unreachable; the corrected ones live in the VM's host ~/.lamin, written by
# cloud-init's lamin-connect.sh, which applies LAMIN_DB_HOST_OVERRIDE.
#
# LAMIN_HOME reaches compose from hub-database/.env, which is where a host path belongs. Naming
# it on the command line still works and still wins, because make exports a command-line variable
# and compose prefers the environment over --env-file, so this stays correct on the VM:
#
#   make schema-plan LAMIN_HOME=/home/ubuntu/.lamin
#
# There is deliberately no `-v` here any more. It bound the same source at the same target as the
# compose bind, so it was a second place for one path to be written down and could only ever
# drift.
SCHEMA_RUN   = $(DB_COMPOSE) run --rm python-lamindb python -m clinicalomx.vocab
SCHEMA_REVIEW_RUN = $(DB_COMPOSE) run --rm python-lamindb \
                    python /workspace/review/schema_review.py
DEFAULT_STORAGE ?=

# The declared foundation is a floor. Each consumer declares what it uses above it and
# advertises that through the clinicalomx.vocabulary entry point (docs/architecture/decisions/0003). plan and
# apply reconcile the FOUNDATION ONLY unless told otherwise, because an operator seeding the
# floor should not silently write another repo's declarations.
#
#   make schema-plan WITH_CONSUMERS=1
#
# widens both to every consumer installed IN THE IMAGE, which is what provisioning a fresh
# instance end to end needs. One variable rather than a second pair of targets: it composes
# with LAMIN_HOME and DEFAULT_STORAGE, and there is no consumers-aware target that can drift
# from its plain counterpart.
#
# An entry point is only visible if the package is installed, so a consumer that is not in
# Dockerfile.lamindb is silently absent here rather than reported missing. schema-audit names
# an owner per feature and is the way to see that.
#
# Deliberately NOT wired into schema-check: check is the CI gate on the floor, and a consumer
# owns its own layer through its ensure().
#
# ONLY 1, true, yes and on enable consumers, in any capitalisation. Everything else is OFF:
# unset, 0, false, no, off, and any unrecognised value or typo. The unknown case must fail
# CLOSED because this flag widens what schema-apply WRITES to the live instance, so a
# misspelled "on" has to under-apply rather than silently write another repo's declarations.
#
# The fold is $(subst) rather than a $(shell) call to tr: make builds the export environment
# after parsing, so a parse-time $(shell) cannot see a command-line WITH_CONSUMERS at all, and
# interpolating the value into a shell string would make it injectable. Only the letters used
# by the truthy words need folding; a value that is not one of them stays unrecognised.
#
# The value must be ONE word as well as a recognised one, because $(filter) matches word by
# word: without the count, WITH_CONSUMERS="yes no" would match on its first word and enable.
WITH_CONSUMERS ?=
WITH_CONSUMERS_LC := $(subst T,t,$(subst R,r,$(subst U,u,$(subst E,e,$(subst Y,y,$(subst S,s,$(subst O,o,$(subst N,n,$(WITH_CONSUMERS)))))))))
CONSUMERS_FLAG = $(if $(and $(filter 1,$(words $(WITH_CONSUMERS_LC))),$(filter 1 true yes on,$(WITH_CONSUMERS_LC))),--with-consumers,)

db-build:
	rm -rf hub-database/dist
	for p in clinicalomx clinicalomx_markers clinicalomx_ledger clinicalomx_reagents; do \
	    uv build --wheel --out-dir hub-database/dist ../$$p || exit 1; \
	done
	$(DB_COMPOSE) build python-lamindb

schema-plan:
	$(SCHEMA_RUN) plan $(CONSUMERS_FLAG) $(if $(DEFAULT_STORAGE),--default-storage $(DEFAULT_STORAGE),)

# The one schema verb that writes, and so the one that takes the guard. plan, check, audit and
# review read only, and reading the connected instance is legitimate wherever it lives.
#
# A PREREQUISITE rather than a line in the recipe, so it runs before the container starts and
# cannot be reached after an apply has begun. hub-database/scripts/schema_write_guard.sh carries
# the whole argument, including why it is an allowlist and why the VM is unaffected.
schema-write-check:
	@sh hub-database/scripts/schema_write_guard.sh "make schema-apply"

schema-apply: schema-write-check
	$(SCHEMA_RUN) apply $(CONSUMERS_FLAG) $(if $(DEFAULT_STORAGE),--default-storage $(DEFAULT_STORAGE),)

schema-check:
	$(SCHEMA_RUN) check

schema-audit:
	$(SCHEMA_RUN) audit --strict

schema-version:
	$(DB_COMPOSE) run --rm python-lamindb python -c "import clinicalomx; print(clinicalomx.__version__)"

# The review page for an apply: what was declared against what landed, a bounded sample of rows
# with lamin_api's validators run over them, and a place for the verdict. hub-database/review.
#
# Same container arrangement as the schema-* verbs above, for the same reason: the declaration
# is inside the image and the corrected instance settings are in the HOST ~/.lamin on the VM, so
# LAMIN_HOME composes here exactly as it does there.
#
# hub-database/ is already mounted at /workspace, so the script and the output path both resolve
# without a second mount. The output lands under .lavish/, which is gitignored at any depth and
# is where an artifact for review belongs: it is a rendering of state recorded elsewhere.
#
# NOT a gate. This exits zero on drift, because make schema-check is the gate and a reviewer
# that fails the build is one nobody runs before the apply. The verdict is on the page.
SCHEMA_REVIEW_OUT ?= .lavish/schema-review.html
SCHEMA_REVIEW_ROWS ?= 20
SCHEMA_REVIEW_REGISTRIES ?=

schema-review:
	$(SCHEMA_REVIEW_RUN) --out /workspace/$(SCHEMA_REVIEW_OUT) --rows $(SCHEMA_REVIEW_ROWS) \
	    $(foreach r,$(SCHEMA_REVIEW_REGISTRIES),--registry $(r))
	@echo ""
	@echo "open it with: lavish-axi hub-database/$(SCHEMA_REVIEW_OUT)"

pipelines-up: network pipelines-env-check
	$(PIPE_COMPOSE) up -d

pipelines-down:
	$(PIPE_COMPOSE) stop

pipelines-nuke:
	$(PIPE_COMPOSE) down -v

pipelines-status:
	$(PIPE_COMPOSE) ps

# --- Operations surface -----------------------------------------------------
# hub-ops/.env carries one secret, the Grafana admin password, and is still not stored in
# Barbican, unlike the other two stacks: the rest is a port, the VM address and a socket path.
# *.env is gitignored, so the real file is absent on a fresh clone and on the VM; materialise it
# from the committed example on first use rather than failing on --env-file. The copy is
# VERBATIM, so the example ships a placeholder rather than a working password and ops-env-check
# refuses to start the stack until it is replaced. See hub-ops/environment.example, which also
# explains why the schema is not called .env.example.
hub-ops/.env:
	cp hub-ops/environment.example $@

# Bind-backed volumes: Docker's local driver does NOT create the directory, it fails to start the
# container if it is missing, and each image runs as a different non-root uid so an existing but
# root-owned directory fails just as hard. Doing this by hand has bitten twice, so it is a
# prerequisite rather than a line in the runbook. Idempotent, and only uses sudo when something
# actually needs changing so a correct host prompts for nothing.
OPS_DIRS = $(OPS_LOKI_DIR):10001 $(OPS_GRAFANA_DIR):472 $(OPS_PROMETHEUS_DIR):65534
OPS_LOKI_DIR       ?= /mnt/data/artifacts/loki
OPS_GRAFANA_DIR    ?= /mnt/data/artifacts/grafana
OPS_PROMETHEUS_DIR ?= /mnt/data/artifacts/prometheus

ops-dirs:
	@for spec in $(OPS_DIRS); do \
	    dir=$${spec%:*}; uid=$${spec##*:}; \
	    if [ -d "$$dir" ] && [ "$$(stat -c %u "$$dir")" = "$$uid" ]; then continue; fi; \
	    if [ ! -d "$$dir" ]; then mkdir -p "$$dir" 2>/dev/null || sudo mkdir -p "$$dir" || exit 1; fi; \
	    if [ "$$(stat -c %u "$$dir")" != "$$uid" ]; then \
	        chown -R "$$uid:$$uid" "$$dir" 2>/dev/null \
	          || sudo -n chown -R "$$uid:$$uid" "$$dir" 2>/dev/null \
	          || sudo chown -R "$$uid:$$uid" "$$dir" \
	          || { echo "ERROR: $$dir must be owned by uid $$uid for its container to start."; \
	               echo "       run: sudo chown -R $$uid:$$uid $$dir"; exit 1; }; \
	    fi; \
	    echo "prepared $$dir (uid $$uid)"; \
	done

# A .env is created ONCE from its example and never updated, so a variable added to the example
# later is silently absent and its compose default applies instead. That has already cost two
# debugging sessions: a Prometheus volume in the wrong place, and cAdvisor pointed at the wrong
# Docker data-root, both presenting as "no data" rather than as a config error. This compares the
# KEYS and refuses to start on a mismatch, which is the same drift guard the UI fixtures have.
#
# One script for all three stacks; it takes the stack directory as its argument. It lives under
# hub-ops because that is where the class was first paid for.
ops-env-check: hub-ops/.env
	@python3 hub-ops/env-check.py hub-ops

# The other two stacks have no rule creating their .env: both hold real secrets and are fetched
# from Barbican on the VM, so materialising one from the example would produce a file full of
# placeholders that looks populated. The check reports the absence instead.
db-env-check:
	@python3 hub-ops/env-check.py hub-database

pipelines-env-check:
	@python3 hub-ops/env-check.py hub-pipelines

ops-up: network hub-ops/.env ops-env-check ops-dirs ops-alert-rules
	$(OPS_COMPOSE) up -d --remove-orphans
	@# The Caddyfile is a bind mount, so Caddy reads it only at startup, and compose does not
	@# recreate the container when just that file changed. Without this a routing change is
	@# silently ignored and the new path 404s through to the file server. Reload rather than
	@# restart: this proxy is in the HPC submission path.
	@$(MAKE) --no-print-directory ops-reload

# Dashboards are generated, not hand-written JSON, and the output is committed. Provisioning
# picks up the file within 30s, so there is no need to restart Grafana.
ops-dashboards:
	python3 hub-ops/grafana/dashboards/build.py

# Alert rules are a template plus a site value, so unlike the dashboards the output is NOT
# committed: it is rendered here, and by clinicalomxdb.service, before Grafana reads it.
ops-alert-rules:
	@python3 hub-ops/grafana/alerting/render.py

# Recreate the proxy, which a reload cannot do for itself. Needed only when the running process
# predates a change to the global options block, the admin endpoint above being the case that
# actually happened. This DROPS connections briefly, and the proxy carries HPC job reporting, so
# it is a separate target rather than something ops-up does silently.
# The Explore tab's tests, plus the schema review's. Its own image tag rather than the compose
# service, because the tests must NOT see the host ~/.lamin: they build throwaway instances and a
# mounted one would put the real instance in reach of a test fixture. The review tests need it
# absent for a second reason: they must never import lamindb, and a connected instance would let
# an accidental import succeed on this machine and fail on the next.
# The Makefile is mounted because test_ops_reload_signal.py includes it and calls
# reload_signal_or_fail directly. A single-FILE bind is what this repo warns against for a
# long-lived service reading a config; this container is created and removed per run, so the
# pinned inode is the one the run started with, which is the point.
# The four /ops-* mounts beside the schema review one are for test_ops_bootstrap_paths.py, which
# copies each widget bootstrap into a throwaway release layout and runs it there. One mount per
# tested directory rather than hub-ops whole, because hub-ops holds a .env and the test does not
# need it. /ops-grafana is for test_alert_rules.py, which renders the alert rules template.
explorer-test:
	docker run --rm \
	    -v $(PWD)/tests/explorer:/tests:ro \
	    -v $(PWD)/hub-database/explorer:/explorer:ro \
	    -v $(PWD)/hub-database/review:/review:ro \
	    -v $(PWD)/hub-database/scripts:/scripts:ro \
	    -v $(PWD)/hub-ops/schema-review:/ops-schema-review:ro \
	    -v $(PWD)/hub-ops/health:/ops-health:ro \
	    -v $(PWD)/hub-ops/login:/ops-login:ro \
	    -v $(PWD)/hub-ops/ttyd:/ops-ttyd:ro \
	    -v $(PWD)/hub-pipelines/prefect/workers/hpc:/ops-hpc:ro \
	    -v $(PWD)/hub-ops/grafana:/ops-grafana:ro \
	    -v $(PWD)/Makefile:/workspace/Makefile:ro \
	    clinicalomxdb-lamindb:local python /tests/run.py

ops-caddy-recreate:
	$(OPS_COMPOSE) up -d --force-recreate caddy
	@sleep 2
	@$(MAKE) --no-print-directory ops-reload

# A make variable so the allow below can sit on the matched line: inside the recipe, make joins
# the continued lines and any shell comment would swallow the rest of the loop.
GRAFANA_PROVISIONING_RELOAD = sh -c 'for p in alerting dashboards datasources; do \
    curl -sf -o /dev/null -X POST "http://localhost:3000/api/admin/provisioning/$$p/reload" \
    -u "$$GF_SECURITY_ADMIN_USER:$$GF_SECURITY_ADMIN_PASSWORD" || exit 1; done' # gitleaks:allow: env var names only

ops-reload: ops-alert-rules
	@# Every one of these reads its config from a bind mount, so compose does NOT recreate the
	@# container when only that file changed, and the change is silently ignored. That has now
	@# caused several "it did not take effect" debugging sessions: a Caddy route 404ing, a Caddy
	@# route 502ing to a service that had been deleted, and Alloy still running an old metrics
	@# pipeline. Reload rather than restart where the component supports it, because Caddy is in
	@# the HPC submission path.
	@#
	@# A FAILED reload is reported as a failure and stops the target. An earlier version collapsed
	@# "container is not running" and "reload was rejected" into the same reassuring message, which
	@# is how a stale Caddy config survived a deploy that looked clean.
	@# The admin endpoint the reload talks to is a STARTUP property of the running process, so a
	@# proxy started before that line existed in the Caddyfile cannot be reloaded into having it.
	@# There is no way out of that by reloading; it needs one recreate, which the hint names.
	@$(call assert_config_visible,clinicalomxdb_caddy,caddy,/etc/caddy/Caddyfile,hub-ops/caddy/Caddyfile)
	@$(call assert_config_visible,clinicalomxdb_alloy,alloy,/etc/alloy/config.alloy,hub-ops/alloy/config.alloy)
	@$(call assert_config_visible,clinicalomxdb_prometheus,prometheus,/etc/prometheus/prometheus.yml,hub-ops/prometheus/prometheus.yml)
	@$(call reload_or_fail,clinicalomxdb_caddy,caddy,\
	    caddy reload --config /etc/caddy/Caddyfile,\
	    connection refused on 2019 means this proxy started before the admin endpoint was \
	    configured. Recreate it once with: make ops-caddy-recreate)
	@# Alloy by SIGNAL, not over HTTP. Its image contains no HTTP client whatsoever, not even
	@# busybox, so the exec-based reload could never have worked; it failed on every deploy and the
	@# old target reported that as "not running". Prometheus and Grafana do ship one, so they keep
	@# the HTTP path, which reports acceptance directly rather than by inference from a log.
	@$(call reload_signal_or_fail,clinicalomxdb_alloy,alloy,failed to reload config,\
	    the collector kept its previous pipeline. Fix config.alloy and run make ops-reload again)
	@$(call reload_or_fail,clinicalomxdb_prometheus,prometheus,\
	    wget -q -O- --post-data='' http://localhost:9090/-/reload)
	@# Grafana reads provisioning at startup only, so an edited dashboard or alert rule is
	@# otherwise invisible until the container is recreated. The admin credentials are already in
	@# the container's own environment, so nothing is passed in from here and nothing lands in the
	@# host process list.
	@$(call reload_or_fail,clinicalomxdb_grafana,grafana,$(GRAFANA_PROVISIONING_RELOAD))

ops-down:
	$(OPS_COMPOSE) stop

ops-status:
	$(OPS_COMPOSE) ps

ops-logs:
	$(OPS_COMPOSE) logs -f --tail 100

# The health endpoint is a HOST systemd unit, not a container (ADR 0004 Decision 5): it needs
# the ssh binary, ~/.ssh and the ControlMaster sockets, and systemctl. Idempotent.
ops-health:
	bash hub-ops/health/bootstrap.sh

# The schema review renderer, a HOST systemd unit for the same reason: it runs make schema-review,
# which drives docker compose, and it serves the last page it rendered at /schema-review/ on the
# portal. Nothing re-renders on its own; the trigger is a button on that tab. Idempotent.
# docs/runbooks/schema-review.md is the procedure.
ops-schema-review:
	bash hub-ops/schema-review/bootstrap.sh

ops-schema-review-status:
	systemctl status ops-schema-review --no-pager -n 20 || true
	@curl -fsS --unix-socket /run/ops-schema-review/review.sock \
	    http://localhost/schema-review/status \
	    | python3 -m json.tool || echo "endpoint not answering; see systemctl status above"

# --- Portal UI development loop ---------------------------------------------
# Local only: serves hub-ops/site against fixture data so every state the portal can render is
# reachable, including failures that cannot be produced on real infrastructure. The dev client is
# injected as the page is served, so hub-ops/site/index.html is never modified to support it and
# nothing dev-only can reach the VM. See hub-ops/devserver/serve.py.
UI_DEV_PORT ?= 8090

ui-dev:
	UI_DEV_PORT=$(UI_DEV_PORT) python3 hub-ops/devserver/serve.py

ui-shots:
	UI_DEV_PORT=$(UI_DEV_PORT) bash hub-ops/devserver/shots.sh

ui-notes:
	@test -s hub-ops/devserver/notes.jsonl \
	  && python3 -c "import json,sys;[print('-',d['state'],'|',d['selector'],'\n   ',d['note']) for d in map(json.loads,open('hub-ops/devserver/notes.jsonl'))]" \
	  || echo "no queued notes"

ui-fixtures-check:
	python3 hub-ops/devserver/check_fixtures.py

# --- Per-user HPC socket broker (Phase 3) ---------------------------------
# The user list is hub-pipelines/prefect/workers/hpc/users.conf, the single source of truth
# for both halves. Run hpc-users FIRST: ops-ssh refuses to start a terminal for an alias that
# does not resolve yet. Both are idempotent and safe to re-run after editing users.conf.
hpc-users:
	bash hub-pipelines/prefect/workers/hpc/bootstrap-users.sh

# The login relay (widgets) and the terminal (fallback) are both installed: the relay is the
# primary path, the terminal is what you fall back to if it mis-reads a prompt. See docs/architecture/decisions/0005.
ops-login:
	bash hub-ops/login/bootstrap.sh

ops-ssh:
	bash hub-ops/ttyd/bootstrap.sh

hpc-users-status:
	@systemctl list-units --all 'hpc-ssh-pool-worker@*' ops-ttyd.service --no-pager --no-legend || true
	@ls -l /home/ubuntu/.ssh/cm-* 2>/dev/null || echo "no ControlMaster sockets: nobody has completed DUO"

ops-health-status:
	systemctl status ops-health --no-pager -n 20 || true
	@curl -fsS --unix-socket /run/ops-health/health.sock http://localhost/health \
	    | python3 -m json.tool || echo "endpoint not answering; see systemctl status above"

# The tunnel runs on the OPERATOR machine, not the VM, so it reads hub-ops/.env in a shell
# instead of through docker compose (`set -a; . file; set +a` is the idiom the worker
# bootstrap already uses). Its own ControlMaster is what makes a second invocation instant
# and lets tunnel-close address exactly this session rather than pkill by pattern.
# Expose a model server running on YOUR machine to the VM, for the Agent tab. Run this on the
# operator machine, alongside `make tunnel`.
#
# Reverse, not forward: the model is on your machine and the consumer is on the VM.
#
# The bind address is the VM's docker bridge, NOT its loopback, and that is the whole difficulty.
# The explorer is a container, so its 127.0.0.1 is its own namespace; a tunnel bound to the VM's
# loopback is invisible to it. Binding to the bridge makes it reachable from every container on
# the host and from nothing off it.
#
# REQUIRES `GatewayPorts clientspecified` in the VM's /etc/ssh/sshd_config. The default refuses
# any bind address but loopback and the tunnel silently lands there instead. "clientspecified"
# rather than "yes" so the address must be named explicitly and never defaults to every interface.
MODEL_PORT ?= 8000
tunnel-model: hub-ops/.env
	@set -a; . hub-ops/.env; set +a; 	gw=$$(ssh "$$OPS_SSH_TARGET" "docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'" 2>/dev/null); 	if [ -z "$$gw" ]; then echo "ERROR: could not read the VM's docker bridge address"; exit 1; fi; 	echo "binding $$gw:$(MODEL_PORT) on the VM -> 127.0.0.1:$(MODEL_PORT) here"; 	ssh -N -f -M -S "$$OPS_TUNNEL_SOCKET-model" 	    -R "$$gw:$(MODEL_PORT):127.0.0.1:$(MODEL_PORT)" "$$OPS_SSH_TARGET" && 	echo "" && 	echo "In the Agent tab, model connection:" && 	echo "  base URL   http://host.docker.internal:$(MODEL_PORT)/v1" && 	echo "  model      the name vLLM serves; check with /v1/models" && 	echo "  API key    anything, it is unused" && 	echo "" && 	echo "If the tab reports a connection error, check GatewayPorts clientspecified on the VM." 	|| echo "tunnel failed; is one already open? make tunnel-model-close"

tunnel-model-close: hub-ops/.env
	@set -a; . hub-ops/.env; set +a; 	ssh -S "$$OPS_TUNNEL_SOCKET-model" -O exit "$$OPS_SSH_TARGET" 2>/dev/null 	    && echo "model tunnel closed" || echo "no model tunnel open"

tunnel: hub-ops/.env
	@set -a; . hub-ops/.env; set +a; \
	if ssh -S "$$OPS_TUNNEL_SOCKET" -O check "$$OPS_SSH_TARGET" 2>/dev/null; then \
	    echo "Tunnel already up. Portal: http://localhost:$$OPS_PORTAL_PORT/"; \
	else \
	    ssh -N -f -M -S "$$OPS_TUNNEL_SOCKET" \
	        -L "$$OPS_PORTAL_PORT:localhost:$$OPS_PORTAL_PORT" "$$OPS_SSH_TARGET" && \
	    echo "Portal: http://localhost:$$OPS_PORTAL_PORT/"; \
	fi

tunnel-close: hub-ops/.env
	@set -a; . hub-ops/.env; set +a; \
	ssh -S "$$OPS_TUNNEL_SOCKET" -O exit "$$OPS_SSH_TARGET" 2>/dev/null \
	    || echo "No tunnel to close."

all-up: network db-up pipelines-up ops-up

all-down: db-down pipelines-down ops-down

# db-up carries db-env-check and runs before the connect, so a .env missing a key stops this before
# anything is written.
#
# The Explore tab is the one surface this does not finish. db-up starts the explorer, which
# imports lamindb, and lamindb resolves the connected instance once at import and caches it; at
# that moment LAMIN_HOME is still empty, so the tab holds the synthetic none/none for the life of
# the process even after the connect that follows writes the directory. `docker restart
# clinicalomxdb_explorer` is what picks it up, and docs/runbooks/operations-ui.md says the same.
fresh-start: network db-up lamin-connect

# BOTH REFUSALS ARE NAMED HERE, ahead of lamin-close, and the order is the whole point. make runs
# prerequisites left to right, so a refusal reachable only through lamin-connect fires after the
# disconnect: an invocation that was always going to refuse would remove current_instance.env from
# the shared host ~/.lamin, taking the instance from the Explore tab, the review app and every
# schema-* verb at once, and leaving `make lamin-connect` unable to put it back because it refuses
# for the same reason. db-env-check is one such refusal and lamin-home-check is the other, so
# neither may be left to the recipe. A third one added later belongs on this line too.
#
# That is the limit of what this ordering can cover, and the limit matters more than the fix.
# lamin-close still runs before the connect, so a .env that passes both checks buys nothing: a
# connect that fails for a reason no prerequisite can predict, an expired LAMIN_API_KEY or an
# unreachable LaminHub, has already cost the shared connection by the time it fails. On the VM that
# leaves the Explore tab, the review app and every schema-* verb without an instance until the
# credential is fixed, with nothing here to put it back. Connecting before closing is what actually
# removes the window, and is filed as separate work.
reconnect: db-env-check lamin-home-check lamin-close lamin-connect
