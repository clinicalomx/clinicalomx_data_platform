"""The declarative environment: lamindb instance, Project, Storage and Services.

Declared once per project in ``manifest.yaml`` and loaded with :func:`load_manifest`, which owns
the process-global lamindb connection. ``InputRef`` and stage configs assume it has connected.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from clinicalomx.settings import YamlSettings


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str
    project_description: str
    _ln: Any | None = PrivateAttr(default=None)

    def provision(self):
        # Assumes the Manifest has connected the instance.
        if self._ln is None:
            import lamindb as ln
            self._ln = (
                ln.Project.filter(name=self.project_name).one_or_none()
                or ln.Project(name=self.project_name, description=self.project_description).save()
            )
        return self._ln


class Storage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_root: str
    region: str
    _ln: Any | None = PrivateAttr(default=None)

    def provision(self):
        # The Manifest establishes the connection.
        if self._ln is None:
            import lamindb as ln
            try:
                self._ln = ln.Storage.get(root=self.storage_root, region=self.region)
            except ln.Storage.DoesNotExist:
                self._ln = ln.Storage(root=self.storage_root, region=self.region).save()
        return self._ln

    def is_accessible(self) -> bool:
        """Check through fsspec that the storage root is mounted here. lamindb does not check it."""
        storage = self.provision()
        marker = storage.path / ".lamindb" / "storage_uid.txt"
        try:
            return marker.fs.exists(str(marker))
        except Exception:
            return False


class Service(BaseModel):
    """An external service endpoint, such as MLflow, from an env var or a literal URI."""
    model_config = ConfigDict(extra="forbid")
    uri_env: str | None = None
    uri: str | None = None

    def resolve(self) -> str | None:
        if self.uri_env:
            val = os.environ.get(self.uri_env)
            if val:
                return val
        return self.uri

    def require(self, label: str = "service") -> str:
        val = self.resolve()
        if not val:
            raise RuntimeError(
                f"{label} not configured: set uri or env var {self.uri_env!r}"
            )
        return val


class Manifest(YamlSettings):
    instance: str
    project: Project
    storage: Storage
    services: dict[str, Service] = Field(default_factory=dict)
    _connected: bool = PrivateAttr(default=False)

    def connect(self) -> None:
        if not self._connected:
            import lamindb as ln
            ln.connect(self.instance)
            self._connected = True

    def is_accessible(self) -> bool:
        self.connect()
        return self.storage.is_accessible()

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError as e:
            raise KeyError(f"No service {name} in manifest.services") from e

    def mlflow_uri(self) -> str:
        return self.service("mlflow").require("mlflow")


def load_manifest(manifest_path: str | os.PathLike, *, cls: type[Manifest] = Manifest) -> Manifest:
    """Parse a manifest YAML into a validated :class:`Manifest` or subclass.

    Precedence, highest first: init kwargs, env (the subclass's prefix), ``.env``, YAML.
    """
    p = Path(manifest_path).resolve()
    # pydantic-settings reads a missing file as empty YAML, so check the path first.
    if not p.is_file():
        raise FileNotFoundError(f"manifest not found: {p}")
    return cls.from_yaml(p)
