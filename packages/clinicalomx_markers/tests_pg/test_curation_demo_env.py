"""The demo runner's app keeps the redirected HOME and lamin settings; only two caches escape."""

from __future__ import annotations

import importlib.util
import pathlib

RUNNER = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "dev" / "curation_demo.py"


def _runner():
    spec = importlib.util.spec_from_file_location("curation_demo", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_app_sees_the_operator_home_only_in_the_two_caches(tmp_path, monkeypatch):
    runner = _runner()
    operator = tmp_path / "operator"
    redirected = tmp_path / "throwaway" / "home"
    settings = tmp_path / "throwaway" / "settings"
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    caches = runner.reflex_caches()
    monkeypatch.setenv("HOME", str(redirected))
    monkeypatch.setenv("LAMIN_SETTINGS_DIR", str(settings))

    env = runner.app_env(caches, tokens_file=tmp_path / "tokens.json",
                         database="localhost:5432/demo", url="http://localhost:8602")

    assert env["HOME"] == str(redirected)
    assert env["LAMIN_SETTINGS_DIR"] == str(settings)
    assert env["CURATION_REQUIRE_DATABASE"] == "localhost:5432/demo"
    assert env["BUN_INSTALL_CACHE_DIR"] == str(operator / ".bun" / "install" / "cache")
    assert pathlib.Path(env["REFLEX_DIR"]).is_relative_to(operator)
    escaped = {k for k, v in env.items() if str(operator) in v}
    assert escaped == {"REFLEX_DIR", "BUN_INSTALL_CACHE_DIR"}
