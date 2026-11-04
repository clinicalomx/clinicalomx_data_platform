# The data layer: environment, inputs, gates

Three things you declare in YAML or Python to run a pipeline stage against lamindb: the environment (`Manifest`), the inputs (`InputRef`) and the human stops (gates).
They work the same under Prefect, cron or a bare script.
Prefect is the workflow scheduler the pipelines use.

Install: see the README's [install section](../README.md#install).
There is no separate `dataspec` distribution; the extras are `clinicalomx[prefect]` and `clinicalomx[models]`.

## 1. Environment - `Manifest`

1. Declare the lamindb instance, Project, Storage and Services once, in a `manifest.yaml`:

   ```yaml
   instance: "owner/name"
   project:
     project_name: "my-project"
     project_description: "..."
   storage:
     storage_root: /hpc/scratch/me/lamindb
     region: my-hpc
   services:
     mlflow:
       uri_env: MLFLOW_TRACKING_URI
   ```

2. Load it at the start of your stage:

   ```python
   from clinicalomx import load_manifest

   m = load_manifest("configs/manifest.yaml")
   assert m.is_accessible()          # connect, then probe that the storage backend is mounted
   uri = m.mlflow_uri()
   ```

The manifest owns the lamindb connection; everything else assumes it has already connected.
`is_accessible()` checks for a marker file on the storage root, so a job on an HPC node without that storage mounted fails fast.

Values layer **init kwargs > env > `.env` > yaml**.
To give your project its own env prefix, subclass:

```python
from pydantic_settings import SettingsConfigDict
from clinicalomx import Manifest as _Base, load_manifest

class Manifest(_Base):
    model_config = SettingsConfigDict(env_prefix="CROI_", env_nested_delimiter="__",
                                      extra="forbid", protected_namespaces=())

def load(p): return load_manifest(p, cls=Manifest)
```

The base env prefix is `DATASPEC_`, and `CONFIG_DIR_ENV` is `DATASPEC_CONFIG_DIR`.
Live configuration uses these names; do not rename them.

## 2. Inputs - `InputRef`

An `InputRef` is `ln.<Registry>.get(...)` written as YAML.
It names ONE object (any lamindb registry, or a served `model`) and resolves it only when you call it.

```yaml
inputs:
  thumbnails: {kind: collection, key: clinical-roi/thumbnails}
  notes:      {kind: artifact,   key: labels/notes.json, source: notes.json}
  source:     {kind: artifact,   uid: 3UAPNgKvvELVVlky0000}
  tma:        {kind: record,     query: {name: TMA, is_type: true}}
  detector:   {kind: model,      objective: general, alias: roi_detection}
```

```python
import clinicalomx as cx

ref = cx.InputRef(kind="record", query={"name": "TMA", "is_type": True})
tma = ref.get()                          # -> ln.Record, or raises
cx.InputRef(kind="artifact", key="labels/notes.json").ensure("notes.json")
```

Use exactly one selector per ref:

| Shape | For |
|---|---|
| `uid` | an exact object and version. Works for every registry |
| `key` (+ optional `version`) | the keyed registries: Artifact, Collection, Transform |
| `query` | a dict of `.get()` kwargs, for name-keyed or typed registries with no key |

- `get()` reads, and raises unless exactly one object matches.
- `ensure()` makes the registered artifact under `key` match the bytes at a local path. It works only for `kind: artifact`.
- An input chosen by a multi-match filter or ranked by a metric is a query, not a ref. Keep it as a plain parameter.

The `model` kind resolves through a callable your project registers:

```python
from clinicalomx import register_model_resolver

register_model_resolver(lambda **coord: load_model(coord.pop("objective", None), **coord))
```

## 3. Gates - `clinicalomx.review`

A gate parks a Record in a `*_pending` state until a human confirms it.
Confirming advances the state and emits an event that the next stage's orchestrator listens for.

1. Choose an event emitter once, at startup:

   ```python
   from clinicalomx import set_state, confirm, register_event_emitter, prefect_emitter

   register_event_emitter(prefect_emitter)        # opt in to Prefect, or inject your own
   ```

2. Park the record at the end of your stage:

   ```python
   set_state(tma, "general_pending")              # the stage parks the record: this is the STOP
   ```

3. After a human reviews the ROIs, confirm:

   ```python
   confirm(tma, "general_approved")               # advance, and emit "general_approved"
   ```

The state lives in the `pipeline_state` feature (`clinicalomx.review.STATE_FEATURE`).
Query it:

```python
cx.get_state(record)                                  # -> the state string, or None
cx.list_by_state(some_queryset, "general_pending")    # -> a list, narrowed in SQL when it can be
```

`roi.one_state_per_record` checks the value stays single.
`clinicalomx[prefect]` is needed only for the built-in emitter.
