"""Cancelling a submitted flow run.

Each monitor poll reads the run's state, scancels on CANCELLING, and marks the run Cancelled
once the job leaves the queue.
"""
from __future__ import annotations

import pytest
from test_oom_retry import (
    _async,
    _FakeClient,
    _FlowRun,
    _Logger,
    _State,
    make_config,
    make_worker,
    run_async,
)

CANCELLING = _State("Cancelling", cancelling=True)


class _Cluster:
    """One SLURM job: live in `state` for `polls` squeue reads or until scancelled."""

    def __init__(self, state="PENDING", polls=1000, ends="COMPLETED", scancel_fails=0):
        self.state, self.polls, self.ends = state, polls, ends
        self.scancel_fails = scancel_fails
        self.scancels: list[str] = []
        self.gone = False

    def install(self, worker):
        async def _squeue_state(job_id):
            if self.gone:
                return None
            self.polls -= 1
            if self.polls < 0:
                self.gone = True
                return None
            return self.state

        async def _sacct_state(job_id):
            return self.ends if self.gone else self.state

        async def _scancel(job_id):
            if self.scancel_fails:
                self.scancel_fails -= 1
                raise RuntimeError("ssh: ControlMaster gone")
            self.scancels.append(job_id)
            self.gone, self.ends = True, "CANCELLED by 1234"

        async def _job_id_by_name(name):
            return None if self.gone else "28900001"

        async def _submit(*_):
            return "28900001"

        worker._squeue_state = _squeue_state
        worker._sacct_state = _sacct_state
        worker._scancel = _scancel
        worker._job_id_by_name = _job_id_by_name
        worker._submit_with_retry = _submit
        worker._registry_env = lambda c: _async(None, {})
        worker._resolve_sif = lambda c: _async(None, None)
        worker._build_script = lambda *a, **k: "#!/bin/bash\n"


def _run(cluster, client, max_poll_failures=5):
    log = _Logger()
    w = make_worker(
        _client=client,
        poll_interval_pending=0,
        poll_interval_running=0,
        max_poll_failures=max_poll_failures,
    )
    w.get_flow_run_logger = lambda flow_run: log
    cluster.install(w)
    return run_async(w.run(_FlowRun(), make_config())), log


def _written(client):
    return [(st.type.value, force) for st, force in client.written]


def test_a_cancelling_run_is_scancelled_once_and_marked_cancelled():
    cluster = _Cluster("PENDING")
    client = _FakeClient([_State("Pending"), CANCELLING, CANCELLING])

    result, _ = _run(cluster, client)

    assert cluster.scancels == ["28900001"]
    assert _written(client) == [("CANCELLED", True)]
    # Nonzero would make the base worker propose Crashed.
    assert result.status_code == 0
    assert result.slurm_state.startswith("CANCELLED")


def test_a_run_that_is_not_cancelling_is_left_alone():
    cluster = _Cluster("RUNNING", polls=3)
    states = [_State("Pending"), _State("Running"), _State("Completed", final=True)]
    client = _FakeClient(states)

    result, _ = _run(cluster, client)

    assert cluster.scancels == []
    assert client.written == []
    assert result.status_code == 0
    assert result.slurm_state == "COMPLETED"


def test_an_unreadable_state_is_not_a_cancel():
    cluster = _Cluster("PENDING", polls=2)
    client = _FakeClient(raises=RuntimeError("API unreachable"))

    result, _ = _run(cluster, client)

    assert cluster.scancels == []
    assert client.written == []
    assert result.slurm_state == "COMPLETED"


def test_a_failed_scancel_is_retried_and_cancelled_waits_for_the_job_to_go():
    cluster = _Cluster("PENDING", scancel_fails=1)
    client = _FakeClient([CANCELLING] * 4)

    result, log = _run(cluster, client)

    assert cluster.scancels == ["28900001"]  # the second attempt
    assert any("scancel 28900001 failed" in w for w in log.warnings), log.warnings
    assert _written(client) == [("CANCELLED", True)]
    assert result.status_code == 0


def test_a_scancel_that_never_lands_never_claims_cancelled():
    """A scancel that never lands leaves the run CANCELLING."""

    class _Unreachable(_Cluster):
        def install(self, worker):
            super().install(worker)
            calls = {"n": 0}

            async def _squeue_state(job_id):
                calls["n"] += 1
                if calls["n"] > 3:
                    raise RuntimeError("ssh: ControlMaster gone")
                return self.state

            worker._squeue_state = _squeue_state

    cluster = _Unreachable("PENDING", scancel_fails=1000)
    client = _FakeClient([CANCELLING] * 10)

    with pytest.raises(RuntimeError, match="ControlMaster"):
        _run(cluster, client, max_poll_failures=1)

    assert cluster.scancels == []
    assert client.written == []


def test_a_running_job_is_scancelled_on_the_first_cancelling_poll():
    cluster = _Cluster("RUNNING")
    client = _FakeClient([_State("Pending"), CANCELLING, CANCELLING])

    result, _ = _run(cluster, client)

    assert cluster.scancels == ["28900001"]
    assert client.reads == 3  # one poll Pending, one poll Cancelling, then run()'s check
    assert _written(client) == [("CANCELLED", True)]
    assert result.status_code == 0


def test_a_hand_scancel_closes_a_cancelling_run():
    """An operator's manual scancel closes a CANCELLING run as Cancelled."""
    cluster = _Cluster("RUNNING", polls=2, ends="CANCELLED by 1234")
    client = _FakeClient([_State("Pending"), _State("Running"), CANCELLING])

    result, _ = _run(cluster, client)

    assert cluster.scancels == []
    assert _written(client) == [("CANCELLED", True)]
    assert result.status_code == 0


def test_a_scancel_nobody_asked_for_is_still_a_failure():
    """A job cancelled outside Prefect, on a run nobody cancelled, is a failure."""
    cluster = _Cluster("RUNNING", polls=2, ends="CANCELLED by 1234")
    client = _FakeClient([_State("Running")] * 4)

    result, _ = _run(cluster, client)

    assert client.written == []
    assert result.status_code == 1


def test_a_job_only_sacct_shows_is_scancelled_on_cancelling():
    """A job only sacct shows is still scancelled."""

    class _SacctFirst(_Cluster):
        def install(self, worker):
            super().install(worker)

            async def _squeue_state(job_id):
                return None

            async def _sacct_state(job_id):
                return self.ends if self.gone else self.state

            worker._squeue_state = _squeue_state
            worker._sacct_state = _sacct_state

    cluster = _SacctFirst("PENDING")
    client = _FakeClient([CANCELLING, CANCELLING])

    result, _ = _run(cluster, client)

    assert cluster.scancels == ["28900001"]
    assert _written(client) == [("CANCELLED", True)]
    assert result.status_code == 0
