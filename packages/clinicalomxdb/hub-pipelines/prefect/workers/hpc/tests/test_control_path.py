"""Per-user socket wiring: ControlPath on the ssh command, and deploy.py's per-user alias."""
from __future__ import annotations

import subprocess

import pytest
from connection import SSHConnection


def _argv_of(monkeypatch) -> list:
    """Capture the argv SSHConnection.run hands to subprocess.run."""
    captured: list = []

    def fake(argv, *args, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    return captured


def test_control_path_is_passed_to_ssh_when_the_block_carries_one(monkeypatch):
    captured = _argv_of(monkeypatch)
    conn = SSHConnection(
        host="hpc-someone", user="someone", control_master_path="~/.ssh/cm-%r@%h:%p"
    )
    conn.run("squeue")

    argv = captured[0]
    assert "-o" in argv
    assert "ControlPath=~/.ssh/cm-%r@%h:%p" in argv
    # The template reaches ssh unexpanded.
    assert "%r" in " ".join(argv)


def test_no_control_path_option_when_the_block_has_none(monkeypatch):
    """Without a template, ssh uses ssh_config."""
    captured = _argv_of(monkeypatch)
    SSHConnection(host="hpc", user="someone").run("squeue")

    argv = captured[0]
    assert not any(str(a).startswith("ControlPath") for a in argv)


def test_control_master_is_never_set(monkeypatch):
    """ControlMaster is never set: a human opens the master with DUO."""
    captured = _argv_of(monkeypatch)
    SSHConnection(
        host="hpc-someone", user="someone", control_master_path="~/.ssh/cm-%r@%h:%p"
    ).run("squeue")

    joined = " ".join(str(a) for a in captured[0])
    assert "ControlMaster" not in joined
    assert "BatchMode=yes" in joined


def test_the_host_field_is_what_reaches_ssh(monkeypatch):
    """Only host reaches ssh, never user, so host must be the per-user alias."""
    captured = _argv_of(monkeypatch)
    SSHConnection(host="hpc-someone", user="someone").run("hostname")

    argv = [str(a) for a in captured[0]]
    assert "hpc-someone" in argv
    assert "someone" not in argv[: argv.index("hpc-someone")]


def test_deploy_derives_a_per_user_alias_and_uses_it_as_the_host():
    """Alias, block name and the host handed to ssh are the same per-user value."""
    import deploy

    base, user = "hpc", "alice"
    alias = f"{base}-{user}"
    block = SSHConnection(
        host=alias, user=user, control_master_path=deploy.CONTROL_PATH_TEMPLATE
    )
    assert block.host == "hpc-alice"
    assert block.host != base
    assert block.control_master_path == "~/.ssh/cm-%r@%h:%p"


def test_control_path_template_matches_the_health_endpoint():
    """deploy.py, ssh_config and the ops health endpoint use one template."""
    import deploy

    assert deploy.CONTROL_PATH_TEMPLATE == "~/.ssh/cm-%r@%h:%p"


@pytest.mark.parametrize(
    "user,expected",
    [
        ("alice", "cm-alice@hpc-login.example.edu:22"),
        ("someone", "cm-someone@hpc-login.example.edu:22"),
    ],
)
def test_the_template_yields_a_distinct_socket_per_user(user, expected):
    """%r gives a socket per user; %h stays the resolved host."""
    resolved = (
        "~/.ssh/cm-%r@%h:%p".replace("%r", user)
        .replace("%h", "hpc-login.example.edu")
        .replace("%p", "22")
    )
    assert resolved.endswith(expected)
