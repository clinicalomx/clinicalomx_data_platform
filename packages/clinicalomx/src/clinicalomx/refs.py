"""``InputRef``: a YAML-serialisable handle to a single lamindb object, or a served model."""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# kind -> lamindb registry attribute. ``"model"`` resolves through the registered model resolver.
_REGISTRY_ATTR = {
    "artifact": "Artifact",
    "collection": "Collection",
    "record": "Record",
    "transform": "Transform",
    "run": "Run",
    "ulabel": "ULabel",
    "feature": "Feature",
    "project": "Project",
    "schema": "Schema",
    "storage": "Storage",
}

# Set by the consuming project.
# Signature: resolver(*, alias, version, run_id, uid, key, objective) -> handle.
_MODEL_RESOLVER: Callable[..., Any] | None = None


def register_model_resolver(fn: Callable[..., Any]) -> None:
    """Register the callable that resolves ``kind: model`` input refs. Call once at startup::

        from clinicalomx import register_model_resolver
        from clinical_roi.common.model_ref import load_model

        def _resolve(*, alias=None, version=None, run_id=None, uid=None,
                     key=None, objective=None):
            coord = {k: v for k, v in dict(
                alias=alias, version=version, run_id=run_id, uid=uid, key=key
            ).items() if v is not None}
            return load_model(objective, **coord) if objective else load_model(**coord)

        register_model_resolver(_resolve)
    """
    global _MODEL_RESOLVER
    _MODEL_RESOLVER = fn


class InputRef(BaseModel):
    """A declarative handle to one lamindb object, or a served ``model``. Resolved lazily.

    ``kind`` picks the registry (any key of ``_REGISTRY_ATTR``). Registry kinds take exactly one
    selector:

    - ``uid`` - an exact object or version.
    - ``key``, optionally with ``version`` - keyed registries; ``key`` alone is the latest.
    - ``query`` - ``.get()`` kwargs, such as ``{name: TMA, is_type: true}``.

    ``kind: model`` takes one of ``alias`` / ``version`` / ``run_id`` / ``uid`` / ``key``, plus an
    optional ``objective``, passed to the registered resolver.

    :meth:`get` reads an existing object. :meth:`ensure` registers the bytes at a path under
    ``key`` (artifacts only). Every shape must match exactly one object.
    """
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "artifact", "collection", "record", "transform", "run",
        "ulabel", "feature", "project", "schema", "storage", "model",
    ] = "artifact"
    uid: str | None = None
    key: str | None = None
    version: str | None = None   # qualifies `key` (or a `model` version)
    query: dict[str, Any] | None = None  # .get() kwargs for registries with no key
    source: str | None = None    # on-disk path for ensure(); get() ignores it
    # `model`-kind selectors, passed to the registered resolver:
    alias: str | None = None
    objective: str | None = None  # model family; the resolver's default applies if unset
    run_id: str | None = None

    @model_validator(mode="after")
    def _check_selector(self):
        model_only = {"alias": self.alias, "run_id": self.run_id, "objective": self.objective}
        if self.kind == "model":
            # Exactly one coordinate; objective is an optional qualifier.
            coords = [self.alias, self.version, self.run_id, self.uid, self.key]
            if sum(c is not None for c in coords) != 1:
                raise ValueError(
                    "model input requires exactly one of `alias`/`version`/`run_id`/`uid`/`key`"
                )
            if self.query is not None or self.source is not None:
                raise ValueError("`query`/`source` are not valid for kind: model")
            return self
        # Registry kinds: exactly one of uid / key / query.
        if [bool(self.uid), bool(self.key), self.query is not None].count(True) != 1:
            raise ValueError("input ref requires exactly one of `uid`, `key`, or `query`")
        if self.version is not None and not self.key:
            raise ValueError(
                "`version` qualifies a `key`; use `query` to version a non-keyed registry"
            )
        bad = [k for k, v in model_only.items() if v is not None]
        if bad:
            raise ValueError(f"{bad} are only valid for kind: model")
        return self

    def get(self):
        """The live object: a registry row, or the resolver's return value for ``kind: model``.

        Raises ``DoesNotExist`` or ``MultipleResultsFound`` unless exactly one matches.
        """
        if self.kind == "model":
            if _MODEL_RESOLVER is None:
                raise RuntimeError(
                    "kind: model requires a registered resolver - call "
                    "clinicalomx.register_model_resolver(...) at project startup"
                )
            coord = {
                k: v for k, v in {
                    "alias": self.alias, "version": self.version, "run_id": self.run_id,
                    "uid": self.uid, "key": self.key, "objective": self.objective,
                }.items() if v is not None
            }
            return _MODEL_RESOLVER(**coord)
        import lamindb as ln
        model = getattr(ln, _REGISTRY_ATTR[self.kind])
        if self.query is not None:
            return model.get(**self.query)
        if self.uid:
            return model.get(self.uid)
        if self.version is not None:
            return model.get(key=self.key, version=str(self.version))
        return model.get(key=self.key)

    def ensure(self, path: str | os.PathLike, *, storage=None, force=False):
        """Register the file or directory at ``path`` under this ``key``, or return it.

        A new key creates an Artifact; unchanged content returns the existing version; changed
        content cuts a new version. ``force=True`` cuts a new version even when the bytes are
        unchanged. Only for a plain ``key`` ref of kind ``artifact``. Without ``force``, identical
        bytes registered under another key return that Artifact.
        """
        if self.kind != "artifact":
            raise ValueError(
                "ensure() registers files as Artifacts; other registry kinds "
                "(collection, record, model and so on) are built by the pipeline or resolved "
                "with get(), not reconciled from a path"
            )
        if self.uid is not None or self.version is not None or self.query is not None:
            raise ValueError(
                "ensure() reconciles a `key` to disk; `uid`/`version`/`query` are "
                "immutable read-pins - use get() for those"
            )
        import lamindb as ln
        return ln.Artifact(path, key=self.key, storage=storage, skip_hash_lookup=force).save()
