"""The clinical-roi binding of ``clinicalomx``, shared by both objectives.

Adds the ``CROI_`` env-override prefix, the ``CROI_CONFIG_DIR`` override, validated stage yaml
loading and the preflight every stage runs. Each objective's ``environment`` module builds an
:class:`ObjectiveEnvironment`.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinicalomx import Manifest as _BaseManifest
from clinicalomx import load_manifest as _load_manifest
from clinicalomx import resolve_config as _resolve_config
from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

import croi.config
from croi.config import StageConfig

CONFIG_DIR_ENV = "CROI_CONFIG_DIR"


_SITE_VALUE = re.compile(r"\$\{(\w+)\}")


class UnsetSiteValue(LookupError):
    """A manifest names a site variable that is not set where it is being read."""


def _expand_site_values(value: Any, where: str = "manifest") -> Any:
    # Copied from clinicalomx.vocab.dataio: no published clinicalomx wheel has it.
    if isinstance(value, dict):
        return {k: _expand_site_values(v, f"{where}.{k}") for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def one(match: re.Match) -> str:
        if not os.environ.get(match.group(1)):
            raise UnsetSiteValue(
                f"{where} names {match.group(1)}, which is not set. It is a site value: on the hub "
                "it is in site.env (scripts/site/render.py), and the HPC worker exports it into "
                "every job; anywhere else, export it first."
            )
        return os.environ[match.group(1)]

    return _SITE_VALUE.sub(one, value)


class InstanceManifest(_BaseManifest):
    """Manifest shaped like ``clinicalomx.Manifest``, with ``CROI_`` env overrides.

    ``${NAME}`` site values expand from the environment; an unset one raises.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        env_nested_delimiter="__",
        env_prefix="CROI_",
        protected_namespaces=(),
    )

    @model_validator(mode="before")
    @classmethod
    def _site_values(cls, data: Any) -> Any:
        return _expand_site_values(data)


def load_manifest(manifest_path: str | os.PathLike) -> InstanceManifest:
    """Parse a manifest yaml into a validated clinical-roi :class:`InstanceManifest`."""
    return _load_manifest(manifest_path, cls=InstanceManifest)


def resolve_config(name: str, default_dir: str | os.PathLike):
    """Locate a config file, honouring ``$CROI_CONFIG_DIR``."""
    return _resolve_config(name, default_dir, env_var=CONFIG_DIR_ENV)


def load_config(path: str | os.PathLike, schema: type[StageConfig],
                overrides: dict | None = None) -> dict:
    """Read a stage yaml, deep-merge ``overrides``, validate against ``schema``. See `croi.config.load`."""
    return croi.config.load(path, schema, overrides)


@dataclass(frozen=True)
class StageEnvironment:
    """What every stage needs before it touches the instance."""

    manifest: InstanceManifest
    storage: Any
    project: Any


def preflight(manifest_path: str | os.PathLike, *,
              ensure_roi_schema: Callable[[], None] | None = None,
              mlflow_env: bool = False) -> StageEnvironment:
    """Connect the instance, provision Storage and Project, bootstrap the ROI schema.

    Fails fast if the storage root is not mounted. ``ensure_roi_schema`` None skips the ROI
    bootstrap; training skips it. ``mlflow_env`` exports ``MLFLOW_TRACKING_URI``; a missing or
    unset mlflow service is reported, not raised.
    """
    manifest = load_manifest(manifest_path)
    if not manifest.is_accessible():
        raise OSError("data endpoint not accessible from this environment")
    manifest.connect()
    if mlflow_env:
        try:
            os.environ.setdefault("MLFLOW_TRACKING_URI", manifest.mlflow_uri())
        except (KeyError, RuntimeError) as e:
            print(f"  no mlflow uri from the manifest ({e}); lamindb-side resolution only")
    stage = StageEnvironment(manifest, manifest.storage.provision(), manifest.project.provision())
    if ensure_roi_schema is not None:
        ensure_roi_schema()
    return stage


@dataclass(frozen=True)
class ObjectiveEnvironment:
    """One objective's packaged configs and preflight. ``objective`` is its directory name."""

    objective: str
    configs: Path

    def resolve_config(self, name: str, default_dir: str | os.PathLike | None = None):
        """Locate a config file, honouring ``$CROI_CONFIG_DIR``. Defaults to :attr:`configs`."""
        return resolve_config(name, self.configs if default_dir is None else default_dir)

    def load_config(self, name: str, schema: type[StageConfig],
                    overrides: dict | None = None) -> dict:
        """Read a stage yaml from :attr:`configs`, deep-merge ``overrides``, validate against ``schema``."""
        return load_config(self.resolve_config(name), schema, overrides)

    def preflight(self, manifest_name: str = "manifest.yaml", *, roi_schema: bool = True,
                  mlflow_env: bool = False) -> StageEnvironment:
        """Connect and provision the instance for a stage; see :func:`preflight`.

        ``roi_schema`` is True for data stages; training passes False.
        """
        return preflight(
            self.resolve_config(manifest_name),
            ensure_roi_schema=self._ensure_roi_schema if roi_schema else None,
            mlflow_env=mlflow_env,
        )

    def _ensure_roi_schema(self) -> None:
        import croi.objectives
        from croi.utils import roi_records

        vocab = croi.objectives.get(self.objective).vocabulary()
        roi_records.ensure_roi_types()
        roi_records.ensure_roi_features(vocab.VOCAB, vocab.ROI_SCALARS)

    def resolve_model(self, **coord):
        """Resolve a ``kind: model`` InputRef to this objective's :class:`clinicalomx.ModelRef`."""
        import croi.objectives

        coord.pop("objective", None)  # this binding is the objective
        return croi.objectives.get(self.objective).registry.model_ref(**coord)
