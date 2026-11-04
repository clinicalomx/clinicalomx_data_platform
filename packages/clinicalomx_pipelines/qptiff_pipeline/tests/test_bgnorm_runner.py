"""The bgnorm-runner's bookkeeping helpers: chunk size, crash classification, refund once per
crash."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import load_flow_module

bgnorm = load_flow_module("bgnorm_stage")
_chunk = bgnorm._chunk

T0 = datetime(2026, 7, 1, tzinfo=UTC)


class _State:
    def __init__(self, type_):
        self.type = type_


class _Run:
    """Stand-in for a Prefect FlowRun: the fields the runner reads."""

    def __init__(self, id_, state, *, start_time=None, end_time=None, core_uids=()):
        self.id = id_
        self.name = f"run-{id_}"
        self.state = _State(state)
        self.start_time = start_time
        self.end_time = end_time
        self.parameters = {"core_uids": list(core_uids)}


def crashed_on_submit(id_="a", **kw):
    """A run the worker never managed to sbatch: CRASHED with no start_time."""
    return _Run(id_, "CRASHED", start_time=None, **kw)


def crashed_after_start(id_="b", **kw):
    """A run that ran and was killed (SLURM TIMEOUT or OOM)."""
    return _Run(id_, "CRASHED", start_time=T0, end_time=T0 + timedelta(hours=4), **kw)


# Chunking.


@pytest.mark.parametrize(
    "n_items,max_jobs,cap,expect_chunks,expect_sizes",
    [
        # 101 cores over 8 jobs, capped at 4: 8x4, and the other 69 wait.
        (101, 8, 4, 8, {4}),
        (3, 8, 4, 3, {1}),  # fewer items than jobs: one each
        (10, 8, 4, 8, {1, 2}),  # balanced, still under the cap
        (32, 8, 4, 8, {4}),  # exactly fills the capacity
        (40, 8, 0, 8, {5}),  # cap disabled: 8x5
        (5, 1, 4, 1, {4}),  # a single job takes only what it can hold
    ],
)
def test_chunk_bounds_count_and_size(n_items, max_jobs, cap, expect_chunks, expect_sizes):
    chunks = _chunk(list(range(n_items)), max_jobs, cap)
    assert len(chunks) == expect_chunks
    assert {len(c) for c in chunks} == expect_sizes
    if cap:
        assert all(len(c) <= cap for c in chunks)


def test_chunk_never_yields_an_empty_chunk():
    # An empty chunk would launch bgnorm-core with core_uids=[], which means batch mode.
    for n in range(1, 40):
        for cap in (0, 1, 3, 4):
            assert all(_chunk(list(range(n)), 8, cap)), (n, cap)


def test_chunk_is_contiguous_and_loses_nothing_it_launches():
    items = list(range(101))
    flat = [u for c in _chunk(items, 8, 4) for u in c]
    assert flat == items[:32]  # contiguous prefix, no duplicates, no gaps
    assert len(set(flat)) == len(flat)


def test_chunk_of_nothing_is_nothing():
    assert _chunk([], 8, 4) == []


# Crash classification.


def test_never_started_only_for_a_crash_that_never_ran():
    assert bgnorm._never_started(crashed_on_submit()) is True
    # Ran and was killed: counts as an attempt.
    assert bgnorm._never_started(crashed_after_start()) is False
    # FAILED: the flow raised after its own retries. Counts.
    assert bgnorm._never_started(_Run("c", "FAILED", start_time=T0)) is False
    assert bgnorm._never_started(_Run("d", "COMPLETED", start_time=T0)) is False


def test_state_type_accepts_an_enum_like_value():
    class _Enum:
        value = "crashed"

    assert bgnorm._state_type(_Run("e", _Enum())) == "CRASHED"
    assert bgnorm._state_type(_Run("f", None)) == ""


# Refund idempotency.


def test_refund_fires_once_per_crash():
    run = crashed_on_submit("run-1")
    # First sighting: refund.
    assert bgnorm._should_refund_crash(run, None) is True
    # bgnorm_last_crash_run is stamped; later polls decline.
    assert bgnorm._should_refund_crash(run, "run-1") is False
    # A new never-started crash is a new refund.
    assert bgnorm._should_refund_crash(crashed_on_submit("run-2"), "run-1") is True


def test_refund_declines_for_runs_that_actually_ran():
    assert bgnorm._should_refund_crash(crashed_after_start("run-3"), None) is False
    assert bgnorm._should_refund_crash(None, None) is False


# Newest terminal run per core.


def test_newest_terminal_run_wins_per_core():
    old = _Run("old", "CRASHED", end_time=T0, core_uids=["u1", "u2"])
    new = _Run("new", "CRASHED", end_time=T0 + timedelta(hours=1), core_uids=["u1"])
    other = _Run("other", "FAILED", end_time=T0 + timedelta(hours=2), core_uids=["u3"])

    got = bgnorm._newest_terminal_by_core([old, new, other])  # unordered on purpose
    assert got["u1"] is new  # newest wins
    assert got["u2"] is old  # only `old` covered u2
    assert got["u3"] is other


def test_newest_terminal_run_tolerates_a_missing_end_time():
    # A run with no end or start time must not break the sort. A finished run wins over it.
    no_times = _Run("no-times", "CRASHED", core_uids=["u1"])
    finished = _Run("finished", "CRASHED", end_time=T0, core_uids=["u1"])
    assert bgnorm._newest_terminal_by_core([no_times, finished])["u1"] is finished


def test_newest_terminal_run_ignores_runs_without_core_uids():
    # A batch-mode run covers no specific core.
    batch = _Run("batch", "CRASHED", end_time=T0)
    batch.parameters = {}
    assert bgnorm._newest_terminal_by_core([batch]) == {}


# Log formatting.


def test_fmt_cores_truncates_long_lists():
    assert bgnorm._fmt_cores(["a", "b"]) == "a, b"
    out = bgnorm._fmt_cores([f"c{i}" for i in range(20)], limit=3)
    assert out == "c0, c1, c2, ... (+17 more)"
