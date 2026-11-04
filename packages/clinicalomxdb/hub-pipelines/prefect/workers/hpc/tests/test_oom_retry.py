"""The OOM retry: resubmit only on OUT_OF_MEMORY, double the request, stop at the ceiling,
and log the old and new request every time."""
from __future__ import annotations

import asyncio

import pytest
from config import HpcSlurmJobConfiguration, format_mem, mem_kib
from worker import _next_mem_request, HpcSlurmSSHPushWorker


class _Logger:
    def __init__(self):
        self.warnings, self.errors, self.infos = [], [], []

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))


def make_worker(**attrs):
    """A worker without BaseWorker's setup."""
    w = HpcSlurmSSHPushWorker.__new__(HpcSlurmSSHPushWorker)
    for k, v in attrs.items():
        setattr(w, k, v)
    return w


def make_config(**kwargs) -> HpcSlurmJobConfiguration:
    base = dict(
        account="a_project",
        image="ghcr.io/o/comx-extract:cx0.3.6-bio4e1845a",
        bind_paths=["/scratch"],
        job_name="flow-abc",
        mem="256G",
    )
    base.update(kwargs)
    return HpcSlurmJobConfiguration(**base)


def run_async(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ memory arithmetic


@pytest.mark.parametrize(
    "mem,kib",
    [
        ("512K", 512),
        ("1M", 1024),
        ("64G", 64 * 1024**2),
        ("1T", 1024**3),
        # A bare number is megabytes to SLURM.
        ("4096", 4096 * 1024),
    ],
)
def test_mem_kib_reads_every_slurm_suffix(mem, kib):
    assert mem_kib(mem) == kib


def test_format_mem_prefers_the_largest_exact_suffix():
    assert format_mem(512 * 1024**2) == "512G"
    assert format_mem(1024**3) == "1T"
    assert format_mem(1536 * 1024) == "1536M"  # not an exact number of GiB


def test_mem_kib_refuses_a_shape_slurm_would_not_take():
    with pytest.raises(ValueError):
        mem_kib("512GB")


# ------------------------------------------------------------------ _next_mem_request


def test_doubles_below_the_ceiling():
    assert _next_mem_request("64G", "512G") == "128G"
    assert _next_mem_request("128G", "512G") == "256G"
    assert _next_mem_request("256G", "512G") == "512G"


def test_clamps_to_the_ceiling_rather_than_exceeding_it():
    assert _next_mem_request("384G", "512G") == "512G"


def test_returns_none_at_the_ceiling_so_the_caller_stops():
    assert _next_mem_request("512G", "512G") is None
    assert _next_mem_request("1T", "512G") is None


def test_a_request_that_cannot_double_stops_too():
    """`--mem=0` means the whole node and doubles to itself."""
    assert _next_mem_request("0", "512G") is None
    assert _next_mem_request("0K", "512G") is None


def test_the_ceiling_bounds_the_attempt_count_without_a_counter():
    mem, steps = "1M", 0
    while (nxt := _next_mem_request(mem, "512G")) is not None:
        mem, steps = nxt, steps + 1
        assert steps < 64, "doubling failed to terminate"
    assert mem == "512G"


# ------------------------------------------------------------------ _grow_mem_after_oom


def test_oom_doubles_the_request_and_names_both_numbers():
    w, cfg, log = make_worker(), make_config(mem="256G", oom_retry_max_mem="512G"), _Logger()

    assert w._grow_mem_after_oom(cfg, "28588164", "OUT_OF_MEMORY", log) is True

    assert cfg.mem == "512G"
    assert len(log.warnings) == 1
    line = log.warnings[0]
    assert "256G" in line and "512G" in line, line
    assert "28588164" in line and "OUT_OF_MEMORY" in line, line


@pytest.mark.parametrize(
    "state",
    ["FAILED", "TIMEOUT", "CANCELLED", "CANCELLED by 1234", "NODE_FAIL", "COMPLETED", ""],
)
def test_no_other_terminal_state_is_ever_retried(state):
    w, cfg, log = make_worker(), make_config(mem="256G", oom_retry_max_mem="512G"), _Logger()

    assert w._grow_mem_after_oom(cfg, "28588164", state, log) is False

    assert cfg.mem == "256G"
    assert log.warnings == [] and log.errors == []


def test_no_retry_at_all_without_a_declared_ceiling():
    w, cfg, log = make_worker(), make_config(mem="256G"), _Logger()

    assert w._grow_mem_after_oom(cfg, "28588164", "OUT_OF_MEMORY", log) is False

    assert cfg.mem == "256G"
    assert log.warnings == []


def test_an_oom_at_the_ceiling_stops_and_reports():
    w, cfg, log = make_worker(), make_config(mem="512G", oom_retry_max_mem="512G"), _Logger()

    assert w._grow_mem_after_oom(cfg, "28588164", "OUT_OF_MEMORY", log) is False

    assert cfg.mem == "512G"  # not raised past the ceiling
    assert log.warnings == []
    assert len(log.errors) == 1
    assert "512G" in log.errors[0] and "NOT resubmitting" in log.errors[0]


def test_a_clamped_retry_says_it_was_clamped():
    w, cfg, log = make_worker(), make_config(mem="384G", oom_retry_max_mem="512G"), _Logger()

    assert w._grow_mem_after_oom(cfg, "28588164", "OUT_OF_MEMORY", log) is True

    assert cfg.mem == "512G"
    assert "768G" in log.warnings[0] and "ceiling" in log.warnings[0]


# ------------------------------------------------------------------ the declaration


def test_a_ceiling_below_the_request_is_refused_where_it_is_declared():
    with pytest.raises(ValueError, match="must cover the request"):
        make_config(mem="512G", oom_retry_max_mem="256G")


def test_the_ceiling_takes_the_same_shape_as_mem():
    assert make_config(oom_retry_max_mem="512g").oom_retry_max_mem == "512G"
    with pytest.raises(ValueError):
        make_config(oom_retry_max_mem="512GB")


# ------------------------------------------------------------------ the loop in run()


class _FakeRun:
    """Drive run() over a scripted list of terminal states, recording each submission."""

    def __init__(self, states):
        self.states = list(states)
        self.submitted: list[str] = []  # the mem each sbatch asked for

    def install(self, worker, configuration):
        async def _submit(script, job_name, logger, configuration_):
            self.submitted.append(configuration.mem)
            return f"2858816{len(self.submitted)}"

        async def _monitor(job_id, logger, *_):
            return self.states.pop(0)

        worker._registry_env = lambda c: _async(None, {})
        worker._resolve_sif = lambda c: _async(None, None)
        worker._submit_with_retry = _submit
        worker._monitor = _monitor
        worker._build_script = lambda *a, **k: "#!/bin/bash\n"


def _async(_, value):
    async def _inner():
        return value

    return _inner()


class _FlowRun:
    id = "c163a127-0000-0000-0000-000000000000"


class _State:

    def __init__(self, name="Running", cancelling=False, final=False):
        self.name = name
        self._cancelling, self._final = cancelling, final

    def is_cancelling(self):
        return self._cancelling

    def is_final(self):
        return self._final


class _ServerRun:
    def __init__(self, state):
        self.state = state


class _FakeClient:
    """The worker's Prefect client, scripted. One state per read, Running once exhausted."""

    def __init__(self, states=None, raises=None):
        self.states = list(states or [])
        self.raises = raises
        self.reads = 0
        self.written: list = []  # every state the worker forced onto the run

    async def set_flow_run_state(self, flow_run_id, state, force=False):
        self.written.append((state, force))

    async def read_flow_run(self, flow_run_id):
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        return _ServerRun(self.states.pop(0) if self.states else _State())


def _run(states, client=None, **cfg_kwargs):
    log = _Logger()
    cfg = make_config(**cfg_kwargs)
    w = make_worker(_client=client if client is not None else _FakeClient())
    w.get_flow_run_logger = lambda flow_run: log
    fake = _FakeRun(states)
    fake.install(w, cfg)
    result = run_async(w.run(_FlowRun(), cfg))
    return result, fake, log, cfg


def test_run_resubmits_once_on_oom_then_succeeds():
    result, fake, log, cfg = _run(
        ["OUT_OF_MEMORY", "COMPLETED"], mem="256G", oom_retry_max_mem="512G"
    )

    assert fake.submitted == ["256G", "512G"]
    assert result.status_code == 0
    assert result.slurm_state == "COMPLETED"
    assert any("1 OOM resubmission" in w for w in log.warnings), log.warnings


def test_run_does_not_resubmit_a_plain_failure():
    result, fake, log, cfg = _run(["FAILED"], mem="256G", oom_retry_max_mem="512G")

    assert fake.submitted == ["256G"]
    assert result.status_code == 1
    assert result.slurm_state == "FAILED"


def test_run_stops_at_the_ceiling_instead_of_looping_there():
    result, fake, log, cfg = _run(
        ["OUT_OF_MEMORY"] * 10, mem="64G", oom_retry_max_mem="512G"
    )

    assert fake.submitted == ["64G", "128G", "256G", "512G"]
    assert result.status_code == 1
    assert result.slurm_state == "OUT_OF_MEMORY"
    assert len(log.errors) >= 1


def test_run_keeps_the_first_pid_and_the_deployments_declared_mem():
    """The pid is reported once, and the deployment's declared mem is never changed."""
    started: list[str] = []

    class _Status:
        def started(self, value):
            started.append(value)

    log = _Logger()
    cfg = make_config(mem="256G", oom_retry_max_mem="512G")
    w = make_worker(_client=_FakeClient())
    w.get_flow_run_logger = lambda flow_run: log
    fake = _FakeRun(["OUT_OF_MEMORY", "COMPLETED"])
    fake.install(w, cfg)
    result = run_async(w.run(_FlowRun(), cfg, task_status=_Status()))

    assert started == ["hpc:28588161"]  # the FIRST job, reported once
    assert result.identifier == "hpc:28588162"  # the result names the job that ran
    assert cfg.mem == "512G"
    assert make_config(mem="256G", oom_retry_max_mem="512G").mem == "256G"


def test_run_does_not_resubmit_a_request_that_cannot_grow():
    result, fake, log, cfg = _run(
        ["OUT_OF_MEMORY"] * 10, mem="0", oom_retry_max_mem="512G"
    )

    assert fake.submitted == ["0"]
    assert result.status_code == 1
    assert result.slurm_state == "OUT_OF_MEMORY"
    assert len(log.errors) >= 1


# ------------------------------------------------------- the cancellation window


def test_a_cancelled_run_is_not_resubmitted():
    """A run cancelled between the OOM kill and the next sbatch is closed as Cancelled."""
    cancelling = _State("Cancelling", cancelling=True)
    client = _FakeClient([cancelling, cancelling])
    result, fake, log, cfg = _run(
        ["OUT_OF_MEMORY", "COMPLETED"],
        client=client,
        mem="256G",
        oom_retry_max_mem="512G",
    )

    assert fake.submitted == ["256G"]  # the doubled job was never sent
    assert client.reads == 2
    assert [st.type.value for st, force in client.written] == ["CANCELLED"]
    assert result.status_code == 0  # not proposed Crashed
    assert result.slurm_state == "OUT_OF_MEMORY"
    assert any("abandoned" in w for w in log.warnings), log.warnings


def test_a_run_already_in_a_terminal_state_is_not_resubmitted():
    client = _FakeClient([_State("Cancelled", final=True)])
    result, fake, _, _ = _run(
        ["OUT_OF_MEMORY", "COMPLETED"],
        client=client,
        mem="256G",
        oom_retry_max_mem="512G",
    )

    assert fake.submitted == ["256G"]
    assert result.slurm_state == "OUT_OF_MEMORY"


def test_a_state_that_cannot_be_read_falls_to_not_resubmitting():
    client = _FakeClient(raises=RuntimeError("API unreachable"))
    result, fake, log, _ = _run(
        ["OUT_OF_MEMORY", "COMPLETED"],
        client=client,
        mem="256G",
        oom_retry_max_mem="512G",
    )

    assert fake.submitted == ["256G"]
    assert result.slurm_state == "OUT_OF_MEMORY"
    assert any("NOT resubmitting" in e for e in log.errors), log.errors


def test_a_live_run_still_gets_its_resubmission():
    client = _FakeClient([_State("Running")])
    result, fake, _, _ = _run(
        ["OUT_OF_MEMORY", "COMPLETED"],
        client=client,
        mem="256G",
        oom_retry_max_mem="512G",
    )

    assert fake.submitted == ["256G", "512G"]
    assert result.status_code == 0


def test_the_summary_names_the_largest_request_an_sbatch_actually_carried():
    """The summary names the largest mem a submitted job asked for, not the grown field."""
    client = _FakeClient([_State("Running"), _State("Cancelling", cancelling=True)])
    result, fake, log, cfg = _run(
        ["OUT_OF_MEMORY", "OUT_OF_MEMORY", "COMPLETED"],
        client=client,
        mem="64G",
        oom_retry_max_mem="512G",
    )

    assert fake.submitted == ["64G", "128G"]  # 256G was grown to but never sent
    assert cfg.mem == "256G"  # the field really is one doubling ahead
    summary = [w for w in log.warnings if "to reach" in w]
    assert len(summary) == 1, log.warnings
    assert "mem 64G -> 128G" in summary[0], summary[0]
    assert "256G" not in summary[0], summary[0]
    assert result.slurm_state == "OUT_OF_MEMORY"
