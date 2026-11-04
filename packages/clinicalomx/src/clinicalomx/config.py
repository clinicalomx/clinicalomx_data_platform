"""Config-file location resolution. Values still layer through the ``YamlSettings`` env prefix."""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "DATASPEC_CONFIG_DIR"


def find_repo_root(start: str | os.PathLike | None = None) -> Path:
    """Walk up from ``start`` (default cwd) to the ``pyproject.toml`` directory, else ``start``."""
    here = Path(start or Path.cwd()).resolve()
    for p in (here, *here.parents):
        if (p / "pyproject.toml").is_file():
            return p
    return here


def stage_config_dir(script_file: str | os.PathLike, repo_rel_configs: str) -> Path:
    """The config directory for a stage script.

    The ``configs/`` directory beside the script, else ``<repo_root>/<repo_rel_configs>`` for a
    script copied elsewhere (such as a SLURM composer running it from ``/tmp/$SLURM_JOB_ID``).
    ``$DATASPEC_CONFIG_DIR`` overrides both, through :func:`resolve_config`.
    """
    sibling = Path(script_file).resolve().parent / "configs"
    if sibling.is_dir():
        return sibling
    return find_repo_root() / repo_rel_configs


def resolve_config(
    name: str,
    default_dir: str | os.PathLike,
    *,
    env_var: str = CONFIG_DIR_ENV,
) -> Path:
    """Locate a config file: ``$<env_var>/<name>`` when ``env_var`` is set, else
    ``<default_dir>/<name>``. Raises ``FileNotFoundError`` naming the path."""
    override = os.environ.get(env_var)
    base = Path(override) if override else Path(default_dir)
    p = (base / name).resolve()
    if not p.is_file():
        raise FileNotFoundError(
            f"config not found: {p} ({env_var}={override!r})"
        )
    return p
