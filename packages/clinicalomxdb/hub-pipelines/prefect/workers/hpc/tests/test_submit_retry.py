"""Submission resilience: SSHConnection.run retries, and _submit_with_retry never submitting
one job twice."""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from config import HpcSlurmJobConfiguration
from connection import SSHConnection
from worker import HpcSlurmSSHPushWorker, SubmissionRefused

CONFIG = HpcSlurmJobConfiguration(account="a_project", image="ghcr.io/o/i:t", bind_paths=["/scratch"])


def make_worker(**attrs):
    """A worker without BaseWorker's setup."""
    w = HpcSlurmSSHPushWorker.__new__(HpcSlurmSSHPushWorker)
    w.submit_retry_delay = 0  # keep the tests instant
    for k, v in attrs.items():
        setattr(w, k, v)
    return w


class _Logger:
    def __init__(self):
        self.warnings, self.errors = [], []

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))

    def info(self, msg):
        pass


def run_async(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ SSHConnection.run


def _conn():
    return SSHConnection(host="hpc", user="someone")


def test_run_defaults_to_a_single_attempt(monkeypatch):
    """attempts defaults to 1."""
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="remote command failed on hpc"):
        _conn().run("squeue")
    assert len(calls) == 1


def test_run_retries_then_succeeds(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            return subprocess.CompletedProcess(args, returncode=255, stdout="", stderr="mux fail")
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    result = _conn().run("squeue", attempts=3, retry_delay=0)
    assert result.stdout == "ok"
    assert len(calls) == 3


def test_run_reraises_the_last_error_with_the_reauth_hint(monkeypatch):
    """The re-auth hint survives the retry."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode=255, stdout="", stderr="dead"),
    )
    with pytest.raises(RuntimeError, match=r"Re-auth if the socket is dead: ssh hpc true"):
        _conn().run("sbatch", attempts=2, retry_delay=0)


def test_run_retries_a_timeout(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert _conn().run("squeue", attempts=2, retry_delay=0).stdout == "ok"
    assert len(calls) == 2


# ------------------------------------------------------------- _submit_with_retry


def test_submit_succeeds_first_time_without_checking_squeue():
    sbatch_calls, squeue_calls = [], []

    async def sbatch(script):
        sbatch_calls.append(script)
        return "12345"

    async def by_name(name):
        squeue_calls.append(name)
        return None

    w = make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)
    assert run_async(w._submit_with_retry("#!/bin/bash", "flow-abc", _Logger(), CONFIG)) == "12345"
    assert len(sbatch_calls) == 1
    assert squeue_calls == []  # no wasted round trip on the happy path


def test_submit_retries_then_succeeds():
    attempts = []

    async def sbatch(script):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("remote command failed on hpc: 'sbatch'")
        return "999"

    async def by_name(name):
        return None  # nothing landed

    log = _Logger()
    w = make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)
    assert run_async(w._submit_with_retry("script", "flow-abc", log, CONFIG)) == "999"
    assert len(attempts) == 3
    assert len(log.warnings) == 2  # one per retry


def test_submit_adopts_an_already_queued_job_instead_of_double_submitting():
    """An ssh failure after sbatch may hide a submitted job. A queued job with this run's name is
    adopted instead of resubmitting."""
    attempts = []

    async def sbatch(script):
        attempts.append(1)
        raise RuntimeError("connection closed after sbatch was accepted")

    async def by_name(name):
        assert name == "flow-abc"
        return "26710167"  # it did land

    log = _Logger()
    w = make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)
    assert run_async(w._submit_with_retry("script", "flow-abc", log, CONFIG)) == "26710167"
    assert len(attempts) == 1  # never submitted a second time
    assert any("adopting" in m for m in log.warnings)


def test_submit_resubmits_when_the_squeue_check_itself_fails():
    """A failed squeue check falls through to another attempt."""
    attempts = []

    async def sbatch(script):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("mux_client_request_session: session request failed")
        return "555"

    async def by_name(name):
        raise RuntimeError("squeue unreachable too")

    log = _Logger()
    w = make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)
    assert run_async(w._submit_with_retry("script", "flow-abc", log, CONFIG)) == "555"
    assert len(attempts) == 2
    assert any("could not check for an existing job" in m for m in log.warnings)


def test_submit_gives_up_after_max_attempts_and_says_how_to_recover():
    """After max attempts, the error points at re-authenticating."""
    attempts = []

    async def sbatch(script):
        attempts.append(1)
        raise RuntimeError("Permission denied (publickey,keyboard-interactive)")

    async def by_name(name):
        return None

    log = _Logger()
    w = make_worker(max_submit_attempts=3, _sbatch=sbatch, _job_id_by_name=by_name, _connection=None)
    with pytest.raises(RuntimeError, match="Permission denied"):
        run_async(w._submit_with_retry("script", "flow-abc", log, CONFIG))
    assert len(attempts) == 3
    assert any("ssh" in m and "true" in m for m in log.errors)


# ------------------------------------------------------------- SLURM refusals


def _refusing(stderr):
    attempts = []

    async def sbatch(script):
        attempts.append(1)
        raise RuntimeError(
            "remote command failed on hpc: 'sbatch'\n"
            f"stderr: sbatch: error: Batch job submission failed: {stderr}\nstdout: "
        )

    async def by_name(name):
        return None

    return attempts, make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)


def test_a_qos_the_account_lacks_fails_on_the_first_sbatch_and_names_it():
    """A QoS the account lacks fails on the first sbatch and is named."""
    config = HpcSlurmJobConfiguration(
        account="a_project", image="ghcr.io/o/i:t", bind_paths=["/scratch"], gpu="nvidia_a100_80gb_pcie_1g.10gb", qos="mig"
    )
    assert (config.partition, config.qos) == ("gpu_cuda", "mig")
    attempts, w = _refusing("Invalid qos specification")
    log = _Logger()
    with pytest.raises(SubmissionRefused) as err:
        run_async(w._submit_with_retry("script", "flow-abc", log, config))
    assert len(attempts) == 1
    assert log.warnings == []
    msg = str(err.value)
    for part in ("qos='mig'", "partition='gpu_cuda'", "account='a_project'", "sacctmgr"):
        assert part in msg
    assert "Re-auth" not in msg and log.errors == [msg]


def test_a_transient_controller_error_keeps_the_retry():
    attempts, w = _refusing("Socket timed out on send/recv operation")
    with pytest.raises(RuntimeError) as err:
        run_async(w._submit_with_retry("script", "flow-abc", _Logger(), CONFIG))
    assert not isinstance(err.value, SubmissionRefused)
    assert len(attempts) == 4


def test_a_granted_qos_submits_once_and_is_rendered():
    config = HpcSlurmJobConfiguration(
        account="a_project", image="ghcr.io/o/i:t", bind_paths=["/scratch"], gpu="nvidia_a100_80gb_pcie_1g.10gb", qos="gpu"
    )
    script = HpcSlurmSSHPushWorker._build_script(
        HpcSlurmSSHPushWorker, None, config, registry_env=None, sif=None
    )
    assert "#SBATCH --qos=gpu" in script.splitlines()
    attempts = []

    async def sbatch(submitted):
        attempts.append(submitted)
        return "424242"

    async def by_name(name):
        return None

    log = _Logger()
    w = make_worker(max_submit_attempts=4, _sbatch=sbatch, _job_id_by_name=by_name)
    assert run_async(w._submit_with_retry(script, "flow-abc", log, config)) == "424242"
    assert attempts == [script]
    assert log.warnings == [] and log.errors == []
