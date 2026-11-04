"""bgnorm: background-normalise each detected core's full-resolution field of view.

A reviewer queues multiplex TMAs at triage. ``bgnorm_runner`` (VM) launches and monitors
``bgnorm_core`` jobs (HPC) and advances each TMA. ``bgnorm_core`` writes a bgnormed zarr and a QC
parquet per core and never changes TMA state.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from datetime import UTC, datetime

from prefect import flow, get_run_logger

STAGE = "bgnorm"
_EPOCH = datetime.min.replace(tzinfo=UTC)




def _values(record) -> dict:
    """The Record's current feature values. Each get_values() call is a query."""
    return record.features.get_values()


def _set_feature(record, name: str, value) -> None:
    """Overwrite a scalar feature value on a Record. lamindb features are multi-valued."""
    if _values(record).get(name) is not None:
        record.features.remove_values(name)
    record.features.add_values({name: value})


def _full_res_image(ds):
    """The largest image-bearing level of a scene datatree (the full resolution image)."""
    nodes = [n for n in ds.subtree if "image" in n.ds.variables]
    if not nodes:
        raise ValueError("scene datatree has no image-bearing levels")
    return max(nodes, key=lambda n: n.ds["image"].size).ds["image"]


def _crop_full_res(ds, bounds, *, pad_ratio):
    """Crop the full-res level of a scene datatree to a box, lazily, as (c, y, x).

    ``bounds`` is (x0, y0, x1, y1) in full-res px. Padding grows the box about its centre and is
    clamped to the image.
    """
    import numpy as np

    img = _full_res_image(ds)
    ynm = next(d for d in img.dims if d.lower() == "y")
    xnm = next(d for d in img.dims if d.lower() == "x")
    H, W = int(img.sizes[ynm]), int(img.sizes[xnm])
    x0, y0, x1, y1 = bounds
    if pad_ratio != 1.0:
        m = (pad_ratio - 1.0) * max(x1 - x0, y1 - y0) / 2.0
        x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    X0, X1 = max(0, int(np.floor(x0))), min(W, int(np.ceil(x1)))
    Y0, Y1 = max(0, int(np.floor(y0))), min(H, int(np.ceil(y1)))
    return img.isel({ynm: slice(Y0, Y1), xnm: slice(X0, X1)})


def _core_crop(core, *, pad_ratio, ds_cache):
    """A Core's full-res field of view, lazily, as (c, y, x). The datatree is cached per qptiff."""
    import geopandas as gpd
    import lamindb as ln
    from bioio import BioImage
    from bioio_qptiff import Reader

    vals = core.features.get_values()
    cores_pq = (
        ln.Artifact.get(key=vals["source_artifact"])
        if isinstance(vals["source_artifact"], str)
        else vals["source_artifact"]
    )
    # Match on roi_id where the parquet has it, else on parquet_row. A wrong row bgnorms the wrong
    # part of the slide.
    from clinicalomx import geometry_row

    geom = geometry_row(
        gpd.read_parquet(cores_pq.path),
        roi_id=vals.get("roi_id"),
        parquet_row=vals.get("parquet_row"),
    ).geometry
    if geom is None or getattr(geom, "is_empty", False):
        raise ValueError(f"{core.name}: inferred/missing cell has no geometry to crop")

    qkey = vals["source_image"]
    qptiff = ln.Artifact.get(key=qkey) if isinstance(qkey, str) else qkey
    if qptiff.key not in ds_cache:
        ds_cache[qptiff.key] = BioImage(
            qptiff.path, reader=Reader
        ).reader.xarray_dask_scene_datatree["FullResolution"]
    return _crop_full_res(ds_cache[qptiff.key], geom.bounds, pad_ratio=pad_ratio)


def _ensure_mlflow_experiment_active(tracking_uri: str | None, name: str) -> None:
    """Restore a soft-deleted MLflow experiment, so bgnorm_pixels can reuse its name.

    Best effort: errors are swallowed. No-op when tracking is unset.
    """
    if not tracking_uri:
        return
    try:
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=tracking_uri)
        exp = client.get_experiment_by_name(name)
        if exp is not None and exp.lifecycle_stage == "deleted":
            client.restore_experiment(exp.experiment_id)
            print(f"restored soft-deleted MLflow experiment {name!r}", flush=True)
    except Exception as exc:
        print(
            f"MLflow experiment restore check failed (non-fatal) for {name!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


#: zstd level for every array in the store. Measured: level 5 is the knee on a real core.
_ZSTD_LEVEL = 5

#: Maximum bytes of a core's output one shard may hold. Channels sharing a shard are staged in a
#: buffer of this size and written as one region, so peak memory does not grow with channel count.
_SHARD_BUDGET_BYTES = 1024**3


def _write_bgnormed_zarr(zarr_path: str, channels: list, datasets, *, channel_dim: str = "c") -> dict:
    """Write a bgnormed zarr one single-channel Dataset at a time. Returns the channel failures.

    The first fitted channel declares the store, chunked one channel deep. Only the shard being
    filled is held in memory. The store uses sharding, zstd at ``_ZSTD_LEVEL``, a transpose filter
    on per-pixel axes (``probs``), and flat chunk keys. The role coords and the ``bgnorm_failures``
    attr are written at the end.
    """
    import dask.array as dsa
    import numpy as np
    import xarray as xr
    import zarr
    from zarr.codecs import TransposeCodec, ZstdCodec

    failures: dict = {}
    roles: dict[str, list[int]] = {"signal_component": [], "background_component": []}
    per_shard = 1  # channels per shard; _declare sets it from the first channel's real size
    layout: dict = {}  # variable -> its dims, for rebuilding the staged Dataset
    shard: dict = {}  # variable -> the (per_shard, ...) buffer being filled, reused per shard
    filled = 0  # channels currently staged in `shard`
    first = 0  # channel index `shard[..][0]` belongs to

    def _encoding(var) -> tuple[dict, tuple[int, ...]]:
        """One variable's encoding, plus the shard shape the template has to be chunked at."""
        # Ask zarr for its auto chunking with a channel extent of 1.
        auto = zarr.create_array(
            store={}, shape=var.shape, dtype=var.dtype, chunks="auto"
        ).chunks
        # An axis that is neither channel nor spatial holds several values of one pixel. Chunk it
        # whole and transpose it last, so those values sit together.
        extra = [i for i, d in enumerate(var.dims) if d != channel_dim][:-2]
        chunks = tuple(n if i in extra else c for i, (n, c) in enumerate(zip(var.shape, auto)))
        # A shard extent must be a whole number of chunks.
        shards = tuple(
            per_shard if d == channel_dim else -(-n // c) * c
            for d, n, c in zip(var.dims, var.shape, chunks)
        )
        enc = {
            "chunks": chunks,
            "shards": shards,
            "compressors": [ZstdCodec(level=_ZSTD_LEVEL)],
            "chunk_key_encoding": {"name": "v2", "separator": "."},
        }
        if extra:
            order = [i for i in range(var.ndim) if i not in extra] + extra
            enc["filters"] = [TransposeCodec(order=tuple(order))]
        return enc, shards

    def _declare(ds) -> None:
        """Create the store and the shard buffer, sized to this channel's variables."""
        nonlocal per_shard
        per_channel = sum(int(v.nbytes) for v in ds.data_vars.values()) or 1
        per_shard = max(1, min(len(channels), _SHARD_BUDGET_BYTES // per_channel))
        template, encoding = {}, {}
        for name, var in ds.data_vars.items():
            enc, shards = _encoding(var)
            shape = tuple(
                len(channels) if d == channel_dim else n for d, n in zip(var.dims, var.shape)
            )
            # Chunked at the shard shape: xarray's alignment check reads encoding["chunks"] as the
            # outer grid. Nothing computes this array.
            template[name] = (var.dims, dsa.zeros(shape, dtype=var.dtype, chunks=shards))
            encoding[name] = enc
            layout[name] = var.dims
            shard[name] = np.empty((per_shard, *var.shape[1:]), dtype=var.dtype)
        coords = {d: ds.coords[d] for d in ds.coords if ds[d].dims == (d,) and d != channel_dim}
        coords[channel_dim] = channels
        for role in roles:
            coords[role] = (channel_dim, np.zeros(len(channels), dtype=ds.coords[role].dtype))
        xr.Dataset(template, coords=coords).to_zarr(
            zarr_path, mode="w", compute=False, encoding=encoding
        )

    def _flush() -> None:
        """Write the staged channels as one region, so each shard is written whole."""
        nonlocal filled
        if not filled:
            return
        # A region write may carry only variables that span the region's dimension, so no coords.
        xr.Dataset({n: (layout[n], a[:filled]) for n, a in shard.items()}).to_zarr(
            zarr_path, region={channel_dim: slice(first, first + filled)}
        )
        filled = 0

    def _stage(i: int, ds) -> None:
        """Copy one channel into the shard buffer and flush when the shard is full.

        The copy lets the channel's own Dataset be released at once.
        """
        nonlocal filled, first
        failures.update(ds.attrs.get("bgnorm_failures") or {})
        for role, seen in roles.items():
            seen.append(int(np.ravel(ds.coords[role].values)[0]))
        if filled == 0:
            first = i
        for name, buf in shard.items():
            buf[filled] = ds[name].values[0]  # a collected failure's narrower dtype widens here
        filled += 1
        if filled == per_shard:
            _flush()

    # The store's dtypes must come from a fitted channel: a failed channel returns narrower dtypes.
    # Leading failures wait until the first fitted channel declares the store.
    declared, pending, i = False, [], -1
    # A plain counter, not enumerate: enumerate keeps the last yielded channel alive.
    for ds in datasets:
        i += 1
        if not declared and (ds.attrs.get("bgnorm_failures") or {}):
            pending.append((i, ds))
            continue
        if not declared:
            _declare(ds)
            declared = True
            while pending:
                _stage(*pending.pop(0))
        _stage(i, ds)
        # Release before pulling the next channel, or this one stays in memory through the next
        # channel's bgnorm.
        del ds
    if not declared and pending:
        _declare(pending[0][1])  # every channel failed; the store still has to exist to be swept
        while pending:
            _stage(*pending.pop(0))
    _flush()

    store = zarr.open_group(zarr_path, mode="r+")
    store.attrs["bgnorm_failures"] = failures
    for role, seen in roles.items():
        store[role][:] = np.asarray(seen, dtype=store[role].dtype)
    return failures


def _bgnorm_core_streaming(fov, zarr_path: str, *, config, tracking, channel_dim: str = "c"):
    """Bgnorm a core one channel at a time into ``zarr_path``. Returns ``(summaries, failures)``.

    Same results as a whole-core ``bgnorm_pixels``, with peak memory of one channel. MLflow layout
    is
    one parent run per core with one nested run per channel, through ``MLFLOW_RUN_ID``.
    """
    import os

    import pandas as pd
    from bgnorm import bgnorm_pixels

    channels = list(fov.coords[channel_dim].values)
    frames: list = []
    parent_run_id: str | None = None

    def _per_channel():
        nonlocal parent_run_id
        for i in range(len(channels)):
            if parent_run_id:
                os.environ["MLFLOW_RUN_ID"] = parent_run_id
            one, summary = bgnorm_pixels(
                fov.isel({channel_dim: [i]}).compute(),
                config=config,
                tracking=tracking,
                on_channel_error="collect",
            )
            if tracking is not None and parent_run_id is None:
                import mlflow

                last = mlflow.last_active_run()  # the parent; its channel run ended first
                parent_run_id = last.info.run_id if last is not None else None
            frames.append(summary)
            # bgnorm_pixels squeezes the channel axis out of a single-channel result.
            out = one.expand_dims(channel_dim) if channel_dim not in one.dims else one
            del one  # `out` shares its arrays
            yield out
            # Release `out` after the yield, or it stays alive through the next channel's bgnorm.
            del out

    # Restore MLFLOW_RUN_ID afterwards: a job env may have set it.
    outer_run_id = os.environ.get("MLFLOW_RUN_ID")
    try:
        failures = _write_bgnormed_zarr(zarr_path, channels, _per_channel(), channel_dim=channel_dim)
    finally:
        if outer_run_id is None:
            os.environ.pop("MLFLOW_RUN_ID", None)
        else:
            os.environ["MLFLOW_RUN_ID"] = outer_run_id
    return pd.concat(frames), failures


def _publish_qc_table(core_name: str, qc) -> None:
    """Publish a core's QC metrics as a Prefect table artifact ``bgnorm-qc-<core-slug>``.

    The review app on the VM reads this copy. Non-finite values become null. Best effort: failures
    are swallowed. The parquet keeps the true NaN.
    """
    import math

    from prefect.artifacts import create_table_artifact

    from qptiff_pipeline.domain import keys
    from qptiff_pipeline.domain.hitl import BGNORM_QC_PREFIX

    def _cell(v):
        if isinstance(v, str) or v is None:
            return v
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return f if math.isfinite(f) else None

    try:
        rows = [{k: _cell(v) for k, v in r.items()} for r in qc.round(4).to_dict("records")]
        create_table_artifact(
            key=BGNORM_QC_PREFIX + keys.slug(core_name),
            table=rows,
            description=f"bgnorm QC metrics ({len(rows)} channel(s)) for {core_name}",
        )
    except Exception as exc:
        print(
            f"{core_name}: QC table artifact publish failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


# retries=1: a retry runs inside the same SLURM allocation and uses its walltime. The bgnorm-runner
# is the durable retry.
@flow(name="bgnorm-core", log_prints=True, retries=1, retry_delay_seconds=60)
def bgnorm_core(
    target_directory: str,
    core_uids: list[str] | None = None,
    pad_ratio: float = 1.1,
    mlflow_experiment: str = "bgnorm",
    mlflow_tracking_uri: str | None = None,
    overwrite: bool = False,
    max_runtime_minutes: int = 0,
    # forwarded verbatim to bgnorm.BgNormConfig
    median_filter_radius: int = 3,
    log_cofactor: int = 150,
    n_components: int = 3,
    sample_prop: float = 0.5,
    sampling_seed: int = 0,
    quantile_post_hoc_value: float | None = 0.75,
    compute_bic_model_order: bool = False,
) -> dict:
    """Background-normalise each Core's full-res field of view and log a per-core MLflow experiment.

    Writes two artifacts per core: the bgnormed zarr and a ``bgnorm-qc-<run_id>.parquet`` with one
    row
    per channel. Never changes TMA state.

    core_uids: Core Record uids to bgnorm. Empty means batch mode: every detected Core of a queued
    or
        running TMA that has no bgnormed artifact.
    target_directory: results root on the HPC (a registered lamindb Storage root). Outputs go to
        ``<slide>/<tma>/<core>/``. A re-run adds a new pair and keeps the old one.
    mlflow_experiment: experiment prefix. The per-core experiment is ``{prefix}/{core}``.
    mlflow_tracking_uri: server URI override. Normally None: the worker sets MLFLOW_TRACKING_URI.
        Unset means a local mlruns/.
    max_runtime_minutes: soft time budget, below the SLURM walltime. When spent, the flow flags the
        remaining cores bgnorm_deferred and ends Completed. 0 disables it.
    """
    logger = get_run_logger()

    import os
    import time
    from pathlib import Path

    import lamindb as ln
    from bgnorm import BgNormConfig
    from bgnorm.tracking import TrackingConfig
    from prefect.runtime import flow_run

    # Run-scoped zarr suffix, stable across @flow retries. "local" outside a Prefect run.
    run_id = flow_run.id or "local"

    from clinicalomx import annotate, get_state, governance

    from qptiff_pipeline.domain import governance as declared
    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.domain.keys import bgnorm_experiment_name
    from qptiff_pipeline.lamin.linking import link_input
    from qptiff_pipeline.lamin.registry import get_or_create_artifact, get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("bgnorm")
    # Load the policy table before any resolution: the SIF runs a git clone with no entry point.
    declared.register()
    try:
        # The lamindb Run uid names each core's MLflow parent run, linking MLflow back to lamindb.
        lamin_run_uid = getattr(getattr(ln.context, "run", None), "uid", None) or run_id

        tma_type = get_or_create_type(states.TMA_TYPE)
        core_type = get_or_create_type(states.CORE_TYPE)
        vocab.ensure()  # declares this repo's features (see vocab.py)

        # Cores to process: the given uids, or in batch mode every detected Core of a queued or
        # running TMA that has no bgnormed artifact.
        if core_uids:
            cores = [ln.Record.get(uid=u) for u in core_uids]
        else:
            active_states = {states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING}
            active = {
                t.name for t in ln.Record.filter(type=tma_type).all() if get_state(t) in active_states
            }
            cores = [
                c
                for c in ln.Record.filter(type=core_type).all()
                if c.name.rsplit(":", 1)[0] in active
                and _values(c).get("is_detected") is not False
                and _values(c).get(vocab.BGNORMED) is None
            ]
            logger.info(f"batch mode: {len(cores)} core(s) missing a bgnormed artifact")
        if not cores:
            logger.info("no cores to bgnorm; nothing to do")
            return {"processed": 0, "results": []}

        # MLflow URI: the parameter, else the job env, else a local mlruns.
        tracking_uri = mlflow_tracking_uri or os.environ.get("MLFLOW_TRACKING_URI")
        if tracking_uri:
            logger.info(f"MLflow tracking URI: {tracking_uri}")
        else:
            logger.warning("MLFLOW_TRACKING_URI unset; bgnorm will log to a local mlruns/ store")

        bg_cfg = BgNormConfig(
            median_filter_radius=median_filter_radius,
            log_cofactor=log_cofactor,
            n_components=n_components,
            sample_prop=sample_prop,
            sampling_seed=sampling_seed,
            quantile_post_hoc_value=quantile_post_hoc_value,
            compute_bic_model_order=compute_bic_model_order,
        )

        ds_cache: dict = {}  # qptiff key to lazy FullResolution datatree (cores share a slide)
        results: list[dict] = []
        failed: list[str] = []  # cores that failed (raise at the end so the job shows red)
        deferred: list[str] = []  # cores the time budget ran out before (NOT failures)
        # Soft deadline, checked before each core so the flow never starts a core it cannot finish.
        deadline = time.monotonic() + max_runtime_minutes * 60 if max_runtime_minutes > 0 else None
        for i, core in enumerate(cores):
            if deadline is not None and time.monotonic() > deadline:
                for rest in cores[i:]:
                    _set_feature(rest, vocab.BGNORM_DEFERRED, True)
                    deferred.append(rest.name)
                logger.warning(
                    f"hit the {max_runtime_minutes}min soft time budget after {i}/{len(cores)} "
                    f"core(s); deferring {len(deferred)} to a fresh job: {_fmt_cores(deferred)}"
                )
                break
            cvals = _values(core)
            # This run reached the core, so clear any deferral flag from an earlier run, before the
            # skip below.
            if cvals.get(vocab.BGNORM_DEFERRED) is True:
                _set_feature(core, vocab.BGNORM_DEFERRED, False)
            # Skip an already-bgnormed core unless overwrite. `bgnormed` is written last, after the
            # QC parquet, so its presence means both outputs exist.
            if cvals.get(vocab.BGNORMED) is not None and not overwrite:
                logger.info(f"{core.name}: already bgnormed; skipping (overwrite=False)")
                results.append({"core": core.name, "artifact": None, "skipped": True})
                continue

            # Governance: a bgnormed zarr is a derivative, so `derive` applies. Resolved from the
            # source qptiff key. A core with no source_image is denied.
            src_key = cvals.get("source_image")
            src_key = getattr(src_key, "key", src_key)
            if not isinstance(src_key, str) or not governance.permits_derive(src_key):
                folder = governance.folder_of(src_key) if isinstance(src_key, str) else None
                basis = governance.policy_for(src_key).basis if isinstance(src_key, str) else \
                    "no source_image; cannot resolve a governance policy"
                msg = f"governance: {folder or '<unattributable>'!r} does not permit derive ({basis})"
                logger.info(f"{core.name}: SKIP - {msg}")
                results.append({"core": core.name, "artifact": None, "skipped": True,
                                "denied": msg})
                continue
            try:
                # Lazy: the crop is computed one channel at a time inside the streaming bgnorm.
                fov = _core_crop(core, pad_ratio=pad_ratio, ds_cache=ds_cache)

                # Resolve the zarr path first: channels are written as they are produced.
                src = cvals.get("source_image")
                qptiff = src if isinstance(src, ln.Artifact) else ln.Artifact.get(key=src)
                # Run-scoped key: each run writes its own zarr and keeps the prior one. A @flow
                # retry
                # reuses the run id, so get_or_create stays idempotent.
                key = keys.bgnorm_key(qptiff.key, core.name, run_id)
                zarr_path = f"{target_directory.rstrip('/')}/{key}"
                Path(zarr_path).parent.mkdir(parents=True, exist_ok=True)

                # The core is the MLflow experiment (bgnorm/<core>). Its channels are the runs.
                exp_name = bgnorm_experiment_name(core.name, prefix=mlflow_experiment)
                # Restore the experiment if it was deleted.
                _ensure_mlflow_experiment_active(tracking_uri, exp_name)
                # The run is named for the lamindb Run. Never pass image_name: the bgnorm build in
                # the
                # SIF rejects it.
                tracking = TrackingConfig(
                    run_name=lamin_run_uid,
                    experiment_name=exp_name,
                    tracking_uri=tracking_uri,
                    # Off: the all-channel grids need every channel in memory.
                    log_grids=False,
                )
                summaries, ch_failures = _bgnorm_core_streaming(
                    fov, zarr_path, config=bg_cfg, tracking=tracking
                )

                # A blank channel cannot fit and comes back all-zero, listed in ``bgnorm_failures``.
                # Only a core where nothing fitted fails. Its zarr is left as an orphan for
                # sweep-orphan-bgnorm-zarrs.
                if ch_failures and len(ch_failures) >= len(summaries):
                    raise RuntimeError(
                        f"every channel failed bgnorm ({len(ch_failures)}): {ch_failures}"
                    )
                if ch_failures:
                    logger.warning(
                        f"{core.name}: {len(ch_failures)}/{len(summaries)} channel(s) failed "
                        f"bgnorm; written zeroed and flagged in the zarr's bgnorm_failures "
                        f"attr: {ch_failures}"
                    )

                # The bytes are written, so register them.
                art = get_or_create_artifact(
                    zarr_path,
                    key=key,
                    description=f"bgnormed full-res FOV for {core.name}",
                )
                link_input(art, qptiff)

                # QC table, one row per channel, beside that run's zarr. Written before `bgnormed`.
                qc = summaries.rename_axis("channel").reset_index()
                qc["channel"] = qc["channel"].astype(str)
                qc.insert(0, "core", core.name)
                # Per-channel failure flag and error, next to the metrics.
                qc["bgnorm_failed"] = qc["channel"].isin(ch_failures)
                qc["bgnorm_error"] = qc["channel"].map(ch_failures)
                qc_key = keys.bgnorm_qc_key(qptiff.key, core.name, run_id)
                qc_path = f"{target_directory.rstrip('/')}/{qc_key}"  # parent mkdir'd above
                qc_art = get_or_create_artifact(
                    qc_path,
                    key=qc_key,
                    description=f"bgnorm QC metrics ({len(qc)} channel(s)) for {core.name}",
                    write=lambda d=qc, p=qc_path: d.to_parquet(p, index=False),
                )
                link_input(qc_art, qptiff, art)
                # The Core Record is the ROI, so `annotate`, not `update`: neither input is anchored
                # to this Core. Writes the role on the Core and the ROI back-refs on the artifact.
                annotate(core, qc_art, vocab.BGNORM_QC_FEATURE)
                _publish_qc_table(core.name, qc)  # VM-facing copy for the review app

                # Last: `bgnormed` is the completion marker the runner, the skip and the review app
                # read.
                annotate(core, art, vocab.BGNORMED)
                results.append({"core": core.name, "artifact": key, "qc": qc_key})
                logger.info(f"{core.name}: bgnormed -> {key} (+ {len(qc)}-channel QC table)")
            except Exception as exc:
                failed.append(core.name)
                logger.warning(f"{core.name}: bgnorm failed ({exc})")

        logger.info(
            f"bgnorm-core done: {len(results)} core(s) bgnormed; {len(failed)} failed; "
            f"{len(deferred)} deferred (out of time)"
        )
        if failed:
            # Fail the run so a failed core is visible. The bgnorm-runner resubmits it.
            raise RuntimeError(f"bgnorm-core: failed for core(s): {', '.join(failed)}")
        # Deferred cores do not fail the run: the runner relaunches them without charge.
        return {"processed": len(results), "results": results, "deferred": deferred}
    finally:
        ln.finish()


_ACTIVE_RUN_STATES = ["SCHEDULED", "PENDING", "RUNNING", "PAUSED", "CANCELLING"]
_TERMINAL_RUN_STATES = ["CRASHED", "FAILED"]


def _state_type(run) -> str:
    """A flow run's state type as a plain upper-case string ('CRASHED', 'FAILED', ...)."""
    st = getattr(getattr(run, "state", None), "type", None)
    return str(getattr(st, "value", st) or "").upper()


def _never_started(run) -> bool:
    """True when a terminal bgnorm-core run crashed before its SLURM job was submitted.

    Detected as CRASHED with no ``start_time``. Such a run did no compute and costs no resubmit.
    """
    return _state_type(run) == "CRASHED" and getattr(run, "start_time", None) is None


def _should_refund_crash(run, last_crash_run) -> bool:
    """True when ``run`` is a never-started crash this core has not been refunded for yet.

    ``last_crash_run`` is the core's stored bgnorm_last_crash_run, so each crash refunds once.
    """
    return run is not None and _never_started(run) and str(last_crash_run or "") != str(run.id)


def _ended_at(run):
    """A sortable, tz-aware end timestamp for a flow run, or the epoch when it has none."""
    ts = getattr(run, "end_time", None) or getattr(run, "start_time", None)
    if ts is None:
        return _EPOCH
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _newest_terminal_by_core(runs) -> dict:
    """core uid -> its newest terminal bgnorm-core run."""
    ordered = sorted(runs, key=_ended_at, reverse=True)
    newest: dict = {}
    for r in ordered:
        for uid in (getattr(r, "parameters", None) or {}).get("core_uids") or []:
            newest.setdefault(uid, r)
    return newest


def _publish_manifest(rows: list[dict]) -> None:
    """Publish the runner's in-flight manifest as a Prefect table artifact. Best effort."""
    try:
        from prefect.artifacts import create_table_artifact

        from qptiff_pipeline.domain.hitl import BGNORM_MANIFEST_KEY

        create_table_artifact(
            key=BGNORM_MANIFEST_KEY,
            table=rows,
            description=f"bgnorm-core runs in flight ({len(rows)})",
        )
    except Exception as exc:
        print(
            f"bgnorm-runner: manifest artifact publish failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def _fmt_cores(names, limit: int = 12) -> str:
    """Comma-joined core names, truncated."""
    names = list(names)
    head = ", ".join(str(n) for n in names[:limit])
    return head if len(names) <= limit else f"{head}, ... (+{len(names) - limit} more)"


def _chunk(items: list, max_jobs: int, max_per_chunk: int = 0) -> list[list]:
    """Split items into at most ``max_jobs`` balanced, contiguous, non-empty chunks.

    Each chunk holds at most ``max_per_chunk`` items (0 = no cap). Items beyond that are dropped;
    the caller picks them up on its next poll. 101 cores, max_jobs=8, cap 4 -> 8 chunks of 4.
    """
    if not items:
        return []
    n = max(1, min(int(max_jobs), len(items)))
    if max_per_chunk and int(max_per_chunk) > 0:
        # Take only what the jobs can hold, then balance across them.
        items = items[: n * int(max_per_chunk)]
        n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    out, i = [], 0
    for j in range(n):
        size = k + (1 if j < m else 0)
        out.append(items[i : i + size])
        i += size
    return out


# retries=1: the runner is re-entrant, so a retry re-adopts the batch it was driving.
@flow(name="bgnorm-runner", log_prints=True, retries=1, retry_delay_seconds=60)
def bgnorm_runner(
    tma_uids: list[str] | None = None,
    max_jobs: int = 8,
    max_cores_per_job: int = 4,
    max_resubmit: int = 3,
    max_submit_failures: int = 5,
    poll_seconds: int = 45,
    max_minutes: int = 720,
) -> dict:
    """Long-running bgnorm orchestrator: launch, monitor, advance.

    Each poll it:
      1. claims bgnorm_queued TMAs -> bgnorm_running;
      2. refunds attempts for cores whose newest run never ran (crashed before sbatch, or deferred);
      3. sorts each running TMA's cores into done, permanently failed, or needed;
      4. launches needed cores not already in flight, in chunks, and counts an attempt per core.
         After ``max_resubmit`` a still-missing core is marked bgnorm_core_failed;
      5. advances a TMA with no needed cores: bgnorm_pending if any core is done, else
         bgnorm_failed. A shortfall is recorded on the TMA and alerted to Teams.
    It exits when no queued or running TMAs remain, or after ``max_minutes``.

    Re-entrant: all state comes from the database and Prefect. Started from the review app's "Run
    bgnorm queue" button. To recover a crashed batch, press the button again.

    max_jobs: cap on concurrent bgnorm-core runs.
    max_cores_per_job: cap on cores per bgnorm-core run, sized against the SLURM walltime.
    max_resubmit: resubmits of a core that ran without producing its artifact, before giving up.
    max_submit_failures: never-started crashes a core may be refunded for, before refunds stop.
    """
    logger = get_run_logger()

    import time

    import lamindb as ln
    from clinicalomx import get_state, set_state
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import (
        FlowFilter,
        FlowFilterName,
        FlowRunFilter,
        FlowRunFilterState,
        FlowRunFilterStateType,
    )
    from prefect.client.schemas.sorting import FlowRunSort
    from prefect.deployments import run_deployment

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.lamin.registry import get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run
    from qptiff_pipeline.orchestration.notify import notify_teams

    DEP = "bgnorm-core/pipeline-bgnorm-core"

    _cv = _values
    _set = _set_feature

    track_prefect_run("bgnorm")
    try:
        tma_type = get_or_create_type(states.TMA_TYPE)
        core_type = get_or_create_type(states.CORE_TYPE)
        from qptiff_pipeline.domain import vocab

        vocab.ensure()  # declares this repo's features (see vocab.py)
        only = set(tma_uids) if tma_uids else None
        start = time.monotonic()
        last_manifest: list[dict] | None = None  # log and republish only on change

        def _read_core_runs(client, state_types):
            """bgnorm-core runs in any of ``state_types``, newest-ended first.

            Sorted server-side so the 200-run limit keeps the recent crashes.
            """
            return client.read_flow_runs(
                flow_filter=FlowFilter(name=FlowFilterName(any_=["bgnorm-core"])),
                flow_run_filter=FlowRunFilter(
                    state=FlowRunFilterState(type=FlowRunFilterStateType(any_=state_types))
                ),
                sort=FlowRunSort.END_TIME_DESC,
                limit=200,  # the server caps at 200
            )

        def _in_flight_cores(client):
            """(core uids in a live bgnorm-core run, count of those runs, the runs themselves)."""
            runs = _read_core_runs(client, _ACTIVE_RUN_STATES)
            uids: set[str] = set()
            for r in runs:
                uids.update((r.parameters or {}).get("core_uids") or [])
            return uids, len(runs), runs

        def _terminal_core_runs(client) -> dict:
            """core uid -> its newest terminal (CRASHED or FAILED) bgnorm-core run."""
            return _newest_terminal_by_core(_read_core_runs(client, _TERMINAL_RUN_STATES))

        with get_client(sync_client=True) as client:
            while True:
                pool = list(ln.Record.filter(type=tma_type).all())
                active = [
                    t for t in pool
                    if get_state(t) in (states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING)
                    and (only is None or t.uid in only)
                ]
                if not active:
                    logger.info("bgnorm-runner: no queued/running TMAs left; done")
                    break

                in_flight, n_active_runs, active_runs = _in_flight_cores(client)
                terminal = _terminal_core_runs(client)
                budget = max(0, max_jobs - n_active_runs)  # bgnorm-core runs we may add now
                logger.info(
                    f"poll: {len(active)} active TMA(s), {n_active_runs} bgnorm-core run(s) in "
                    f"flight covering {len(in_flight)} core(s); budget {budget}"
                )

                advanced = 0
                uid_to_name: dict[str, str] = {}  # for the manifest
                for tma in active:
                    if get_state(tma) == states.S.BGNORM_QUEUED:
                        set_state(tma, states.S.BGNORM_RUNNING)  # claim
                    detected = [
                        c for c in ln.Record.filter(name__startswith=f"{tma.name}:R", type=core_type)
                        if _cv(c).get("is_detected") is not False
                    ]
                    uid_to_name.update({c.uid: c.name for c in detected})
                    if not detected:
                        set_state(tma, states.S.BGNORM_FAILED)
                        logger.error(f"{tma.name}: no detected cores -> bgnorm_failed")
                        advanced += 1
                        continue

                    # Refund attempts for runs that did no work: a submit that never happened
                    # (CRASHED, no start_time) or a core the time budget deferred.
                    for c in detected:
                        vals = _cv(c)
                        if vals.get(vocab.BGNORMED) is not None or vals.get(vocab.BGNORM_CORE_FAILED) is True:
                            continue
                        reason = None
                        if vals.get(vocab.BGNORM_DEFERRED) is True:
                            # Clear it here, so later polls do not refund the same deferral.
                            _set(c, vocab.BGNORM_DEFERRED, False)
                            reason = "deferred by the soft time budget"
                        else:
                            run = terminal.get(c.uid)
                            if _should_refund_crash(run, vals.get(vocab.BGNORM_LAST_CRASH_RUN)):
                                _set(c, vocab.BGNORM_LAST_CRASH_RUN, str(run.id))
                                n_sub = int(vals.get(vocab.BGNORM_SUBMIT_FAILURES) or 0) + 1
                                _set(c, vocab.BGNORM_SUBMIT_FAILURES, n_sub)
                                if n_sub <= max_submit_failures:
                                    reason = (
                                        f"run {run.name} crashed before sbatch landed "
                                        f"({n_sub}/{max_submit_failures} submit failure(s))"
                                    )
                                else:
                                    logger.error(
                                        f"{c.name}: {n_sub} submit failure(s) exceeds "
                                        f"max_submit_failures={max_submit_failures}; no longer "
                                        f"refunding; check the HPC SSH socket (ssh hpc true)"
                                    )
                        if reason:
                            attempts = int(_cv(c).get(vocab.BGNORM_ATTEMPTS) or 0)
                            _set(c, vocab.BGNORM_ATTEMPTS, max(0, attempts - 1))
                            logger.warning(
                                f"{c.name}: {reason}; refunding attempt "
                                f"{attempts} -> {max(0, attempts - 1)}"
                            )

                    done = [c for c in detected if _cv(c).get(vocab.BGNORMED) is not None]
                    gave_up, need = [], []
                    for c in detected:
                        vals = _cv(c)
                        if vals.get(vocab.BGNORMED) is not None:
                            continue
                        if vals.get(vocab.BGNORM_CORE_FAILED) is True:
                            gave_up.append(c)
                            continue
                        if int(vals.get(vocab.BGNORM_ATTEMPTS) or 0) > max_resubmit:
                            _set(c, vocab.BGNORM_CORE_FAILED, True)  # exhausted resubmits
                            gave_up.append(c)
                            logger.warning(f"{c.name}: exhausted {max_resubmit} resubmit(s) -> bgnorm_core_failed")
                            continue
                        need.append(c)

                    if not need:
                        # Record the shortfall on the TMA. A partial result still advances.
                        _set(tma, vocab.BGNORM_FAILED_CORES, len(gave_up))
                        if done:
                            set_state(tma, states.S.BGNORM_PENDING)
                            if gave_up:
                                names = _fmt_cores(c.name for c in gave_up)
                                logger.error(
                                    f"{tma.name}: INCOMPLETE -> bgnorm_pending: "
                                    f"{len(done)}/{len(detected)} bgnormed, {len(gave_up)} core(s) "
                                    f"permanently failed: {names}"
                                )
                                notify_teams(
                                    f"bgnorm incomplete: {tma.name}",
                                    f"{len(done)}/{len(detected)} cores bgnormed. "
                                    f"{len(gave_up)} permanently failed after {max_resubmit} "
                                    f"resubmit(s): {names}. The TMA advanced to bgnorm_pending; "
                                    f"re-open the failed cores with the clear-bgnorm-failures flow "
                                    f"or the review app's re-run button.",
                                )
                            else:
                                logger.info(f"{tma.name}: {len(done)}/{len(detected)} bgnormed -> bgnorm_pending")
                        else:
                            set_state(tma, states.S.BGNORM_FAILED)
                            logger.error(f"{tma.name}: all cores failed -> bgnorm_failed")
                            notify_teams(
                                f"bgnorm failed: {tma.name}",
                                f"All {len(detected)} detected core(s) failed to bgnorm; the TMA is "
                                f"at bgnorm_failed. Failed cores: {_fmt_cores(c.name for c in gave_up)}",
                            )
                        advanced += 1
                        continue

                    launchable = [c for c in need if c.uid not in in_flight]
                    waiting = [c for c in need if c.uid in in_flight]
                    logger.info(
                        f"{tma.name}: {len(done)}/{len(detected)} done, {len(gave_up)} failed, "
                        f"{len(waiting)} in flight, {len(launchable)} launchable; budget {budget}"
                    )
                    if budget <= 0 or not launchable:
                        continue

                    by_uid = {c.uid: c for c in launchable}
                    # At most `budget` more runs, at most max_cores_per_job cores in each.
                    chunks = _chunk(
                        [c.uid for c in launchable], min(budget, max_jobs), max_cores_per_job
                    )
                    launched: list[str] = []
                    for chunk in chunks:
                        names = _fmt_cores(uid_to_name.get(u, u) for u in chunk)
                        try:
                            child = run_deployment(
                                name=DEP, parameters={"core_uids": chunk}, timeout=0, as_subflow=True
                            )
                        except Exception as exc:
                            # One failed submit never aborts the poll. The cores are relaunched next
                            # poll, uncharged.
                            logger.error(
                                f"{tma.name}: run_deployment failed for {len(chunk)} core(s) "
                                f"({names}): {type(exc).__name__}: {exc}; retrying next poll"
                            )
                            continue
                        budget -= 1
                        in_flight.update(chunk)
                        launched.extend(chunk)
                        logger.info(
                            f"{tma.name}: launched bgnorm-core "
                            f"{getattr(child, 'name', '?')} ({getattr(child, 'id', '?')}) with "
                            f"{len(chunk)} core(s): {names}"
                        )
                    # Charge only the cores that went out in a launched chunk.
                    for uid in launched:
                        c = by_uid[uid]
                        _set(c, vocab.BGNORM_ATTEMPTS, int(_cv(c).get(vocab.BGNORM_ATTEMPTS) or 0) + 1)

                # Manifest: which bgnorm-core run carries which TMA's cores. Logged and published.
                rows = []
                for r in active_runs:
                    uids = (r.parameters or {}).get("core_uids") or []
                    names = [uid_to_name.get(u, u) for u in uids]
                    tmas = sorted({n.rsplit(":", 1)[0] for n in names if ":" in n})
                    rows.append(
                        {
                            "run": getattr(r, "name", None),
                            "run_id": str(getattr(r, "id", "")),
                            "state": _state_type(r),
                            "tma": ", ".join(tmas) or "?",
                            "n_cores": len(uids),
                            "cores": _fmt_cores(names),
                        }
                    )
                # Only when it changes.
                if rows != last_manifest:
                    for row in rows:
                        logger.info(
                            f"  in flight: {row['run']} [{row['state']}] {row['tma']} "
                            f"({row['n_cores']}): {row['cores']}"
                        )
                    _publish_manifest(rows)
                    last_manifest = rows
                if advanced:
                    logger.info(f"poll: advanced {advanced} TMA(s) out of bgnorm_running")

                if (time.monotonic() - start) > max_minutes * 60:
                    logger.warning("bgnorm-runner: hit max_minutes; exiting (a later run re-adopts)")
                    break
                time.sleep(poll_seconds)

        return {"done": True}
    finally:
        ln.finish()
