# Runbook: reviewing a schema apply

Render a page that compares what the schema declares with what landed in the instance, then act on it.
The page reads through `hub-database/explorer/lamin_api.py`, exits zero even when it finds drift, and never writes data.

## Review production

On your own machine:

1. Open the tunnel:

   ```bash
   make tunnel
   ```

2. Open `http://localhost:8080/#schema`.
3. Read the header. It shows when the page was rendered, and turns amber after an hour.
4. If there is no page, or it is old, press **Re-render** and wait a minute or two.
5. Check that the meta box names the database you meant, as `role@host:port/database`.
6. Read the panels. See [Read the page](#read-the-page).

The page shows a sample of real rows.
Treat it as clinical data.
Everyone with SSH access to the VM can see it.
A reviewer with no SSH access to the VM cannot use it.

## Review on the VM by hand

On the VM:

```bash
cd ~/clinicalomxdb
make db-up
make schema-review LAMIN_HOME=/home/ubuntu/.lamin
```

The page lands at `hub-database/.lavish/schema-review.html`.
It is gitignored.

## Annotate a page

Annotation runs in your own browser.
On your own machine, copy the page from the VM and open it:

```bash
set -a; . hub-ops/.env; set +a
scp "$OPS_SSH_TARGET:clinicalomxdb/hub-database/.lavish/schema-review.html" .
lavish-axi schema-review.html
```

On WSL, add `--no-open` and open the printed URL yourself.

## Review a local instance

On a developer machine, from a clean clone:

1. Copy the environment file:

   ```bash
   cp hub-database/environment.example hub-database/.env
   ```

2. In `hub-database/.env`, fill in `POSTGRES_PASSWORD` and `PGADMIN_DEFAULT_PASSWORD`.
3. Change `LAMIN_API_KEY=CHANGEME` to `LAMIN_API_KEY=`, with nothing after it. Leave the `INDEX_READ_*` pair empty.
4. Start the database and create a local instance:

   ```bash
   make network
   make db-up
   make lamin-local
   ```

5. Apply the schema and render the page:

   ```bash
   make schema-apply
   make schema-review
   ```

6. Open the page. On WSL, add `--no-open`.

   ```bash
   lavish-axi hub-database/.lavish/schema-review.html
   ```

- You need the `clinicalomxdb-lamindb:local` image. `make db-build` needs the private index credential. Without it, get the image from an earlier build or a colleague.
- Use `make lamin-local` here, not `make lamin-connect` or `make fresh-start`. Both of those connect production.
- The first `make lamin-local` needs a route to lamindb's hub, for one storage query. Later runs work offline and take about a second.

### On an existing clone

1. Add the key that an older `.env` lacks:

   ```bash
   printf '\nLAMIN_DB_HOST_OVERRIDE=\n' >> hub-database/.env
   ```

2. Run `make db-up`. It recreates `python-lamindb`.
3. Connect again with `make lamin-local`.

Until step 1, `db-dirs`, `db-certs`, `db-up`, `lamin-connect`, `lamin-local` and `reconnect` fail and name the key.
The other database targets and every `schema-*` target run without it.

### What `make lamin-local` makes

- An instance named `clinicalomxdb-local-review`, owned by lamindb's anonymous user.
- A database named `clinicalomxdb_local_review`, in the cluster `make db-up` started.
- A storage root at `local-review-storage` under `LAMIN_HOME`.

It registers nothing on LaminHub, touches no production database, and needs no credential.
If `LAMIN_HOME` has no `current_user.env`, it writes the anonymous user there.
An existing `current_user.env` is left as it is.
`hub-database/scripts/lamin_local.py` has the details.

### What a fresh local page shows

- Panel 1 reports `DTYPE DISAGREES` for every declared entity type, with a declared uid and a landed uid that differ. This is expected on any instance other than the one the declaration was dumped from. `make schema-apply` reports the same as `UID DRIFT` and `REPAIR`, and `make schema-check` fails on it. Read the feature rows of panel 1, and panel 2.
- `make schema-apply` creates a `.lamindb/storage_uid.txt` under `hub-storage/hpc_scratch/`, at the `storage.hpc_root` path below `/scratch`. It is untracked and safe to delete.

## If the command fails

Before you run it, check:

1. `hub-database/.env` is filled in.
2. The database stack is up: `make db-up`.
3. The image carries `clinicalomx`: `make schema-version` prints its version.
4. An instance is connected.
5. The connected role can read the instance's tables.

| Message | Fix |
|---|---|
| exit 2, `cannot read the instance` | No instance is connected. On the VM, set `LAMIN_HOME` to the host `~/.lamin`. On a developer machine, run `make lamin-local`. |
| exit 2, naming a role and a table | The role may connect but not read. Grant `SELECT` on the instance's tables plus `ALTER DEFAULT PRIVILEGES`, or connect as a role that has both. |
| exit 2, naming a registry | The instance has no registry of that name. Fix `SCHEMA_REVIEW_REGISTRIES`. |
| exit 2, `cannot read the declaration` | The declaration is empty, `features.csv` has no dtype column, or a data file lacks its top-level key. The message names the file and what it found. |
| `psycopg2.OperationalError` | On a developer machine, you connected with `make lamin-connect`. Run `make lamin-local`. |
| a JWT error at `make schema-apply` | Your stored LaminHub login no longer authenticates. Run `lamin login`, or delete `current_user.env` from `LAMIN_HOME` and run `make lamin-local` again. |

When the declaration comes from the installed `clinicalomx` package, all three data files must sit in one directory.
If none is complete, the command names every path it tried and what each one lacked.
A directory you pass with `--declared` may lack a file.

## Read the page

### The meta box

Read it first.
It names the instance slug, and the database that answered, as `role@host:port/database`.
For a sqlite instance it shows the file path.
Check that the database is the one you meant.
It also states how many names the declared side carried.

### Panel 1: declared versus landed

- **GREEN**: every declared name was confirmed to land with its declared dtype.
- **AMBER**: some declared dtypes could not be confirmed either way, or a deprecated declaration is absent. The banner counts them, and the callout above the tables names them.
- **FAILING**: a name did not land, landed with a different dtype, or an entity type was recreated under a new uid.

A name that is present but not declared is reported and never fails.

`unresolved` means the reviewer cannot tell whether the dtype agrees.
A blank declared dtype is also `unresolved`.
`make schema-audit` names the owner.

Do not change a dtype in place once records hold values under it.
Change the declaration and backfill.

If many features show `DTYPE DISAGREES` at once:

1. Check one such feature's stored dtype in pgAdmin or on the Explore tab.
2. The page translates the declaration's `type:<Name>` into the `cat[Record[<uid>]]` form lamindb stores. If the stored spelling differs from that form, fix `compare.normalise_declared_dtype`, not the declaration.
3. Treat the page as unreliable until this is settled.

### Panel 2: sampled rows

- **GREEN**: every sampled feature value was judged, and none was refused.
- **FAILING**: a value was refused. The cell is named.
- **AMBER**: some cells were not judged, a merged feature reached no column, a registry had no cell judged, the cap left a registry you named unsampled, or nothing was judged.

Only feature columns are judged, with `lamin_api`'s own checks.
An empty feature cell is skipped.

A cell is left unjudged when:

- it holds several values, joined with `, `
- its categorical feature has no permitted value landed
- `lamin_api` read the permitted-value list only in part, and the value is not in the part it read

An unjudged cell sits in an amber row and a refused value in a red row, each with the cell marked.
A registry heading reads `NO cell judged` when no cell in it was judged.

### Panel 3: the verdict box

It repeats every gap from the banners and names each registry where nothing was judged.
Then write your verdict.

## Act on an annotation

Each diff line and each sampled row is its own element, so an annotation lands on one thing.

| The page says | Change |
|---|---|
| a name did not land | the declaration in the `clinicalomx` repository, or the apply step that seeds it |
| a dtype disagrees | the declaration, plus a backfill if records hold values under the old dtype |
| a value is flagged | the curation rule in the pipeline that wrote it |
| a storage root did not land | register the storage root |

Then re-apply and render the page again.
Do not correct the instance by hand.

## Options

| Variable | Default | For |
|---|---|---|
| `LAMIN_HOME` | from `hub-database/.env` | the host `~/.lamin` both lamindb services bind; the command line overrides the `.env` value |
| `SCHEMA_REVIEW_OUT` | `.lavish/schema-review.html` | output path, relative to `hub-database/` |
| `SCHEMA_REVIEW_ROWS` | `20` | rows sampled per registry |
| `SCHEMA_REVIEW_REGISTRIES` | unset | registries to sample, space separated; defaults to Artifact and Record |

- At most eight registries are sampled. Name more and it samples the first eight, and the page names the rest.
- A registry you name is sampled even when it is empty.
- An empty default registry is dropped. Name it to see it.

## The portal tab

### Install it

Once per VM, on the VM:

```bash
make ops-schema-review
make ops-up
```

The first installs and starts the `ops-schema-review` host unit.
The second is needed only when Caddy lacks the `/run/ops-schema-review` mount.
Cloud-init installs it on a rebuilt VM.

The bootstrap reads three optional settings:

| Variable | Default | For |
|---|---|---|
| `REVIEW_LAMIN_HOME` | the checkout owner's `~/.lamin`, which is `~ubuntu/.lamin` on the VM | the settings directory the render reads |
| `REVIEW_TIMEOUT` | `900` | seconds a render may take before it is killed and reported failed |
| `REVIEW_STALE_AFTER` | `3600` | seconds after which the header calls the page old |

### What the button does

- It runs `make schema-review` and no other verb. It takes no value from the request.
- A second press while a render runs is refused.
- The new page replaces the old one in one step.
- A failed render keeps the previous page, and says above it that the re-render failed.
- Command output goes to the journal. Read it on the VM with `journalctl -u ops-schema-review`.

`tests/explorer/test_schema_review_server.py` tests these rules.
`docs/runbooks/operations-ui.md` describes the unit.

## The guard on `make schema-apply`

`make schema-apply` refuses unless the connected instance's database is this stack's own Postgres.
`hub-database/scripts/schema_write_guard.sh` reads the `current_instance.env` the run will use, and matches its host and port against an allowlist.

- `schema-plan`, `schema-check`, `schema-audit` and `schema-review` run against whatever is connected.
- On the VM, an apply is allowed. Cloud-init's `lamin-connect.sh` sets `LAMIN_DB_HOST_OVERRIDE=postgres`.
- A forwarded database port is refused, even on loopback.
- `make db-shell` and the Explore tab's Python console do not pass through the guard.

## `LAMIN_HOME`

`LAMIN_HOME` is the settings directory that records which instance is connected.

- It is set in `hub-database/.env` and defaults to `~/.clinicalomxdb/lamin`, which `make db-up` creates.
- Every lamindb container binds it at `/root/.lamin`.
- To share your own `~/.lamin` with this project, set `LAMIN_HOME=~/.lamin` in `hub-database/.env`. The write verbs then target whatever it names.
- `make lamin-connect` and `make lamin-local` chown its files to the owner of the directory above `LAMIN_HOME`, and make them owner-only.
- On the VM, `/usr/local/bin/lamin-connect.sh` chowns `/home/ubuntu/.lamin` to `ubuntu` and changes no modes.
- Do not run these targets with `sudo`. It resets `HOME`, so `~` resolves to another directory.
- After a connect, restart the Explore tab with `docker restart clinicalomxdb_explorer`. The tab cannot query an instance made by `make lamin-local`.

`hub-database/environment.example` documents the `LAMIN_HOME` key.

### Move it from an earlier default

1. In `hub-database/.env`, change the line to `~/.clinicalomxdb/lamin`.
2. Run `make db-up`.
3. Connect again, with `make lamin-connect` or `make lamin-local`.
4. Delete the old directory if this project created it: the in-tree `hub-database/lamin/`, or a `~/.lamin` that existed only for this project.

Do not delete your own `~/.lamin`.
It holds the settings for every lamindb instance on this host.
Remove only this project's files from it.
