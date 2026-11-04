"""bgnorm-core drives bgnorm one channel at a time, and the output does not change.

The AST guards run everywhere. The rest need the ``bgnorm`` extra (python >= 3.12):

    uv run --extra bgnorm pytest qptiff_pipeline/tests/test_bgnorm_streaming.py

The identity tests use real bgnorm: a stand-in would only prove the stand-in is deterministic.
"""
from __future__ import annotations

import ast
import gc
import os
import pathlib
import weakref

import numpy as np
import pytest

from conftest import SRC, load_flow_module

FLOW = SRC / "qptiff_pipeline" / "flows" / "bgnorm_stage.py"


def _missing(*names: str) -> str | None:
    """The first of ``names`` that will not import, as a skip reason."""
    import importlib.util

    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                return f"{name} is in the bgnorm extra"
        except (ImportError, ValueError):
            return f"{name} is in the bgnorm extra"
    return None


needs_zarr = pytest.mark.skipif(
    _missing("xarray", "zarr", "dask") is not None, reason=str(_missing("xarray", "zarr", "dask"))
)
needs_bgnorm = pytest.mark.skipif(
    _missing("xarray", "zarr", "dask", "bgnorm") is not None,
    reason=str(_missing("xarray", "zarr", "dask", "bgnorm")),
)


@pytest.fixture(scope="module")
def stage():
    return load_flow_module("bgnorm_stage")


# Guards that need no compute stack.


def _bgnorm_pixels_calls(path: pathlib.Path) -> list[ast.Call]:
    """Every ``bgnorm_pixels(...)`` call in one module, found by AST."""
    return [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bgnorm_pixels"
    ]


def test_bgnorm_is_only_ever_handed_one_channel():
    calls = _bgnorm_pixels_calls(FLOW)
    assert len(calls) == 1
    image = calls[0].args[0]
    chain, node = [], image
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        chain.append(node.func.attr)
        node = node.func.value
    assert chain == ["compute", "isel"], (
        f"bgnorm_pixels' image argument is {ast.unparse(image)!r}; it has to be a per-channel "
        "isel slice, or peak memory goes back to scaling with the channel count"
    )


def test_the_all_channel_grids_are_switched_off():
    configs = [
        node
        for node in ast.walk(ast.parse(FLOW.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TrackingConfig"
    ]
    assert len(configs) == 1
    log_grids = {kw.arg: kw.value for kw in configs[0].keywords}.get("log_grids")
    assert isinstance(log_grids, ast.Constant) and log_grids.value is False


# The zarr assembly, on real xarray Datasets and a real store.


def _channel_dataset(name: str, *, y: int = 8, x: int = 6, k: int = 3, seed: int = 0, fail=None):
    """One channel's bgnorm output, shaped as ``bgnorm_pixels`` returns it: channel axis squeezed,
    ``c`` and the component coords left as scalars."""
    import xarray as xr

    rng = np.random.default_rng(seed)
    ds = xr.Dataset(
        {
            "adjusted_image": (("y", "x"), rng.random((y, x))),
            "labels": (("y", "x"), rng.integers(0, k, (y, x), dtype=np.uint8)),
            "probs": (("c_probs", "y", "x"), rng.random((k, y, x)).astype(np.float32)),
            "adjusted_image_post_hoc": (("y", "x"), rng.random((y, x))),
        },
        coords={
            "y": np.arange(y),
            "x": np.arange(x),
            "c": name,
            "signal_component": np.int64(-1 if fail else 2),
            "background_component": np.int64(-1 if fail else 1),
        },
    )
    ds.attrs["bgnorm_failures"] = {name: fail} if fail else {}
    return ds.expand_dims("c")


@needs_zarr
def test_the_streamed_store_matches_one_whole_dataset_write(stage, tmp_path):
    import xarray as xr
    import zarr

    names = ["DAPI", "CD4", "CD8", "PanCK"]
    parts = [
        _channel_dataset(n, seed=i, fail="ValueError: blank" if n == "CD8" else None)
        for i, n in enumerate(names)
    ]
    expected = xr.concat(parts, dim="c")
    expected.attrs["bgnorm_failures"] = {"CD8": "ValueError: blank"}
    whole = tmp_path / "whole.zarr"
    expected.to_zarr(whole, mode="w")

    streamed = tmp_path / "streamed.zarr"
    failures = stage._write_bgnormed_zarr(str(streamed), names, iter(parts))
    assert failures == {"CD8": "ValueError: blank"}

    old, new = xr.open_zarr(whole), xr.open_zarr(streamed)
    assert sorted(old.data_vars) == sorted(new.data_vars)
    assert new.attrs["bgnorm_failures"] == old.attrs["bgnorm_failures"]
    for v in old.data_vars:
        assert old[v].dims == new[v].dims and old[v].dtype == new[v].dtype, v
        assert np.array_equal(old[v].values, new[v].values, equal_nan=True), v
    for c in ("c", "y", "x", "signal_component", "background_component"):
        assert np.array_equal(old[c].values, new[c].values), c

    # The one difference: every array is chunked one channel deep.
    store = zarr.open_group(streamed, mode="r")
    assert [store[v].chunks[0] for v in sorted(new.data_vars)] == [1, 1, 1, 1]


@needs_zarr
@pytest.mark.parametrize("per_shard", [1, 2, 4])
def test_the_writer_holds_at_most_one_shard_of_channels(stage, tmp_path, monkeypatch, per_shard):
    import zarr

    names = ["a", "b", "c", "d", "e", "f", "g", "h"]
    parts = {n: _channel_dataset(n, seed=i) for i, n in enumerate(names)}
    per_channel = sum(v.nbytes for v in parts["a"].data_vars.values())
    monkeypatch.setattr(stage, "_SHARD_BUDGET_BYTES", per_channel * per_shard)
    path = tmp_path / "z.zarr"
    checked = []

    def _pull():
        for i, n in enumerate(names):
            written = i - i % per_shard  # everything before the shard being filled
            for j in range(written):
                on_disk = zarr.open_group(path, mode="r")["adjusted_image"][j]
                assert np.array_equal(on_disk, parts[names[j]]["adjusted_image"].values[0]), j
                checked.append(names[j])
            yield parts[n]

    stage._write_bgnormed_zarr(str(path), names, _pull())
    assert zarr.open_group(path, mode="r")["adjusted_image"].shards[0] == per_shard
    assert checked, "the guard never ran"


@needs_zarr
def test_the_store_is_sharded_compressed_and_flat_keyed(stage, tmp_path):
    import xarray as xr
    import zarr

    names = [f"ch{i}" for i in range(6)]
    path = tmp_path / "z.zarr"
    stage._write_bgnormed_zarr(
        str(path), names, (_channel_dataset(n, seed=i) for i, n in enumerate(names))
    )
    store = zarr.open_group(path, mode="r")
    for v in ("adjusted_image", "adjusted_image_post_hoc", "labels", "probs"):
        a = store[v]
        assert a.chunks[0] == 1, f"{v}: a region write needs a one-channel-deep chunk"
        assert a.shards is not None, f"{v}: unsharded, one file per chunk"
        assert a.shards[1:] == a.shape[1:], f"{v}: a shard must span every non-channel axis"
        assert a.metadata.chunk_key_encoding.separator == ".", v
        inner = a.metadata.codecs[0].codecs
        assert any(getattr(c, "level", None) == stage._ZSTD_LEVEL for c in inner), v

    # probs' c_probs axis holds one pixel's posteriors. The chunk must span it.
    probs = store["probs"]
    assert probs.chunks[1] == probs.shape[1]
    order = next(c.order for c in probs.metadata.codecs[0].codecs if hasattr(c, "order"))
    assert order[-1] == 1, "the component axis has to end up last"
    assert not any(
        hasattr(c, "order") for c in store["adjusted_image"].metadata.codecs[0].codecs
    ), "a (c, y, x) array has no axis worth transposing"

    # Reader contract: xarray takes its chunks from the inner grid, and zarr serves partial reads.
    lazy = xr.open_zarr(path)["adjusted_image"]
    assert lazy.chunks[0] == (1,) * len(names)
    assert lazy.encoding["chunks"] == store["adjusted_image"].chunks
    box = lazy.isel(c=slice(1, 3), y=slice(2, 6), x=slice(1, 5)).values
    assert box.shape == (2, 4, 4)


@needs_zarr
@pytest.mark.parametrize("n_channels", [4, 32])
def test_the_shard_buffer_is_capped_and_flat_in_the_channel_count(
    stage, tmp_path, monkeypatch, n_channels
):
    import zarr

    names = [f"ch{i}" for i in range(n_channels)]
    parts = [_channel_dataset(n, seed=i) for i, n in enumerate(names)]
    per_channel = sum(v.nbytes for v in parts[0].data_vars.values())
    monkeypatch.setattr(stage, "_SHARD_BUDGET_BYTES", per_channel * 3)
    stage._write_bgnormed_zarr(str(tmp_path / "z.zarr"), names, iter(parts))
    store = zarr.open_group(tmp_path / "z.zarr", mode="r")
    per_shard = {store[v].shards[0] for v in ("adjusted_image", "labels", "probs")}
    assert per_shard == {3}, "the shard depth must come from the budget, not the channel count"


@needs_zarr
def test_no_channel_is_reachable_once_the_next_one_starts(stage, tmp_path, monkeypatch):
    import sys
    import types

    import pandas as pd
    import xarray as xr

    names = [f"ch{i}" for i in range(6)]
    live: list[tuple[weakref.ref, int]] = []
    retained: list[int] = []
    asked: list[str] = []

    def _base(array):
        """The base array every view of this channel's output shares."""
        while getattr(array, "base", None) is not None:
            array = array.base
        return array

    def _fake_bgnorm_pixels(image, *, config, tracking, on_channel_error):
        gc.collect()  # xarray's internals hold reference cycles
        retained.append(sum(nbytes for ref, nbytes in live if ref() is not None))
        name = str(np.ravel(image.coords["c"].values)[0])
        asked.append(name)
        ds = _channel_dataset(name, seed=len(live)).squeeze("c")
        assert "c" not in ds.dims, "the fake has to hand over the squeezed shape bgnorm returns"
        for var in ds.data_vars.values():
            array = _base(var.variable.data)
            live.append((weakref.ref(array), int(array.nbytes)))
        return ds, pd.DataFrame([{"gmm_means": 1.0}], index=[name])

    probe = _channel_dataset("probe").squeeze("c")
    expanded = probe.expand_dims("c")
    assert all(
        _base(probe[v].variable.data) is _base(expanded[v].variable.data) for v in probe.data_vars
    ), (
        "expand_dims no longer returns a VIEW, so the probe below would go dead at the flow's "
        "`del one` whatever the flow retains, and this test would pass vacuously"
    )
    del probe, expanded

    fake = types.ModuleType("bgnorm")
    fake.bgnorm_pixels = _fake_bgnorm_pixels
    monkeypatch.setitem(sys.modules, "bgnorm", fake)

    fov = xr.Dataset(
        {"image": (("c", "y", "x"), np.zeros((len(names), 4, 4), np.uint16))},
        coords={"c": names, "y": np.arange(4), "x": np.arange(4)},
    )
    stage._bgnorm_core_streaming(fov, str(tmp_path / "z.zarr"), config=None, tracking=None)

    assert asked == names, "every channel has to have been asked for, in order"
    assert len(retained) == len(names)
    assert retained == [0] * len(names), (
        f"bytes of an earlier channel's output still reachable when bgnorm was handed the next: "
        f"{retained}; peak is one channel's working set only while this stays zero"
    )


# The identity proof, against real bgnorm.

#: the flow's own bgnorm parameters (the bgnorm_core signature defaults)
CFG = {
    "median_filter_radius": 3,
    "log_cofactor": 150,
    "n_components": 3,
    "sample_prop": 0.5,
    "sampling_seed": 0,
    "quantile_post_hoc_value": 0.75,
    "compute_bic_model_order": False,
}


def _synthetic_core(channels: list[str], *, blank: tuple[str, ...] = (), size: int = 384):
    """A lazy (c, y, x) uint16 core, as ``_core_crop`` returns one.

    A three-population mixture, large enough to trigger bgnorm's seeded pixel subsampling.
    """
    import dask.array as dsa
    import xarray as xr

    rng = np.random.default_rng(17)
    n = size * size
    planes = []
    for name in channels:
        if name in blank:
            planes.append(np.zeros((size, size), np.uint16))
            continue
        pick = rng.choice(3, n, p=[0.55, 0.30, 0.15])
        v = np.where(
            pick == 0,
            rng.normal(120, 30, n),
            np.where(pick == 1, rng.normal(600, 150, n), rng.normal(3000, 700, n)),
        )
        v[rng.random(n) < 0.08] = 0
        planes.append(np.clip(v, 0, 65535).astype(np.uint16).reshape(size, size))
    return xr.DataArray(
        dsa.from_array(np.stack(planes), chunks=(len(channels), size, size)),
        dims=("c", "y", "x"),
        coords={"c": channels, "y": np.arange(size), "x": np.arange(size)},
        name="core",
    )


def _assert_streaming_matches(stage, tmp_path, channels, blank=()):
    import xarray as xr
    from bgnorm import BgNormConfig, bgnorm_pixels

    cfg = BgNormConfig(**CFG)
    fov = _synthetic_core(channels, blank=blank)

    whole_ds, whole_summaries = bgnorm_pixels(
        fov.compute(), config=cfg, on_channel_error="collect"
    )
    whole = tmp_path / "whole.zarr"
    whole_ds.to_zarr(whole, mode="w")

    summaries, failures = stage._bgnorm_core_streaming(
        fov, str(tmp_path / "streamed.zarr"), config=cfg, tracking=None
    )

    assert failures == whole_ds.attrs["bgnorm_failures"]
    assert list(summaries.index) == list(whole_summaries.index)
    assert list(summaries.columns) == list(whole_summaries.columns)
    assert summaries.equals(whole_summaries)

    old, new = xr.open_zarr(whole), xr.open_zarr(tmp_path / "streamed.zarr")
    assert sorted(old.data_vars) == sorted(new.data_vars)
    assert new.attrs["bgnorm_failures"] == old.attrs["bgnorm_failures"]
    for v in old.data_vars:
        assert old[v].dims == new[v].dims and old[v].dtype == new[v].dtype, v
        assert np.array_equal(old[v].values, new[v].values, equal_nan=True), v
    for c in ("c", "y", "x", "signal_component", "background_component"):
        assert np.array_equal(old[c].values, new[c].values), c


@needs_bgnorm
def test_streaming_reproduces_a_whole_core_bgnorm(stage, tmp_path):
    _assert_streaming_matches(stage, tmp_path, ["DAPI", "CD4", "CD8", "PanCK"])


@needs_bgnorm
def test_streaming_reproduces_a_collected_channel_failure(stage, tmp_path):
    _assert_streaming_matches(stage, tmp_path, ["Blank", "DAPI", "CD4", "CD8"], blank=("Blank",))


@needs_bgnorm
def test_streaming_reproduces_a_core_where_nothing_fitted(stage, tmp_path):
    channels = ["B1", "B2"]
    _assert_streaming_matches(stage, tmp_path, channels, blank=tuple(channels))


@needs_bgnorm
def test_the_core_still_gets_one_mlflow_parent_run(stage, tmp_path):
    mlflow = pytest.importorskip("mlflow", reason="mlflow is in bgnorm's mlflow extra")
    from bgnorm import BgNormConfig
    from bgnorm.tracking import TrackingConfig

    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    experiment = "bgnorm/test-core"
    channels = ["DAPI", "CD4", "CD8"]
    stage._bgnorm_core_streaming(
        _synthetic_core(channels),
        str(tmp_path / "z.zarr"),
        config=BgNormConfig(**CFG),
        # plots off, for speed only
        tracking=TrackingConfig(
            run_name="lamin-run-uid",
            experiment_name=experiment,
            tracking_uri=uri,
            log_plots=False,
            log_grids=False,
        ),
    )
    assert "MLFLOW_RUN_ID" not in os.environ

    client = mlflow.tracking.MlflowClient(tracking_uri=uri)
    runs = client.search_runs([client.get_experiment_by_name(experiment).experiment_id])
    parents = [r for r in runs if r.data.tags.get("mlflow.parentRunId") is None]
    nested = [r for r in runs if r.data.tags.get("mlflow.parentRunId") is not None]
    assert len(parents) == 1
    assert parents[0].data.tags["mlflow.runName"] == "lamin-run-uid"
    assert sorted(r.data.tags["mlflow.runName"] for r in nested) == sorted(channels)
    assert {r.data.tags["mlflow.parentRunId"] for r in nested} == {parents[0].info.run_id}
    assert {r.data.tags["bgnorm.parent_run_id"] for r in nested} == {parents[0].info.run_id}
