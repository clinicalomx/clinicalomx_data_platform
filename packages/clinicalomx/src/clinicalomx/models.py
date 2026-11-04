"""Model pointers: find and fetch a deep-learning model checkpoint.

A model has a lamindb ``ln.Artifact`` (kind="model") and an MLflow registered-model version,
linked by the training run id. :class:`ModelRef` anchors on either and ``.path`` returns a local
checkpoint, downloaded from MLflow when the lamindb copy is not on disk. It never loads the model.
MLflow aliases are the source of truth; lamindb ULabels mirror them. Needs ``clinicalomx[models]``
for the MLflow side.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

# `import lamindb` connects to the instance, so it is imported inside each function.
if TYPE_CHECKING:  # pragma: no cover
    import lamindb as ln

# An alias (such as "production") points at one model version. Match across registries by run id.
DEFAULT_ALIAS = "production"

# Checkpoint downloads, cached per (model, version or run).
_MLFLOW_WEIGHTS_CACHE: dict[str, Path] = {}
_MISSING = object()  # "not yet resolved", distinct from None


def _mlflow():
    """Import mlflow lazily."""
    try:
        import mlflow
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RuntimeError(
            "MLflow resolution requires the optional dependency: install clinicalomx[models]"
        ) from e
    return mlflow


# lamindb alias resolution.
def _alias_type(alias_namespace: str, create: bool = False) -> ln.ULabel | None:
    """The ULabel *type* named ``alias_namespace`` that namespaces alias labels."""
    import lamindb as ln
    t = ln.ULabel.filter(name=alias_namespace, is_type=True).one_or_none()
    if t is None and create:
        t = ln.ULabel(name=alias_namespace, is_type=True).save()
    return t


def _alias_label(alias: str, alias_namespace: str, create: bool = False) -> ln.ULabel | None:
    """The ULabel for one alias, under the namespace type."""
    import lamindb as ln
    atype = _alias_type(alias_namespace, create=create)
    if atype is None:
        return None
    label = ln.ULabel.filter(name=alias, type=atype).one_or_none()
    if label is None and create:
        label = ln.ULabel(name=alias, type=atype).save()
    return label


def resolve_model_artifact(alias: str, alias_namespace: str) -> ln.Artifact | None:
    """The lamindb model Artifact an alias points at, or ``None``."""
    import lamindb as ln
    try:
        label = _alias_label(alias, alias_namespace)
        if label is None:
            return None
        return ln.Artifact.filter(kind="model", ulabels=label).order_by("-created_at").first()
    except Exception:
        return None


def _resolve_lamin_alias(alias: str, alias_namespace: str) -> Path:
    """Local path to the checkpoint the lamindb alias points at. Raises if unset or off-disk."""
    import lamindb as ln
    label = _alias_label(alias, alias_namespace)
    if label is None:
        raise LookupError(f"no lamindb alias {alias!r} in namespace {alias_namespace!r}")
    art = ln.Artifact.filter(kind="model", ulabels=label).order_by("-created_at").first()
    if art is None:
        raise LookupError(f"lamindb alias {alias!r} is not attached to any model artifact")
    p = Path(art.cache())
    if not p.exists():
        raise FileNotFoundError(f"lamindb weights for alias {alias!r} not on disk at {p}")
    return p


# MLflow download.
def _pick_checkpoint(root: Path) -> Path:
    """The checkpoint ``.pt`` under ``root``: ``best.pt``, else one that is not ``last.pt`` or
    ``epoch*.pt``."""
    pts = list(root.rglob("*.pt"))
    if not pts:
        raise FileNotFoundError(f"no .pt checkpoint under {root!r}")
    best = [p for p in pts if p.name == "best.pt"]
    if best:
        return best[0]
    named = [p for p in pts if p.name != "last.pt" and not p.name.startswith("epoch")]
    return (named or pts)[0]


def download_best_pt(source_uri: str) -> Path:
    """Download an MLflow artifact or logged-model URI and return its checkpoint path."""
    mlflow = _mlflow()
    local = Path(mlflow.artifacts.download_artifacts(artifact_uri=source_uri))
    return local if local.is_file() else _pick_checkpoint(local)


def resolve_mlflow_weights(
    registered_model: str,
    *,
    version: str | int | None = None,
    alias: str | None = None,
    run_id: str | None = None,
    model_uri: str | None = None,
) -> Path:
    """Download a checkpoint from MLflow by ``model_uri``, ``version``, ``alias`` or ``run_id``.

    Cached.
    """
    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        raise RuntimeError("MLFLOW_TRACKING_URI not set; cannot reach MLflow registry")
    mlflow = _mlflow()
    mlflow.set_tracking_uri(uri)

    if model_uri is not None:
        ck = f"uri:{model_uri}"
        if ck not in _MLFLOW_WEIGHTS_CACHE:
            _MLFLOW_WEIGHTS_CACHE[ck] = download_best_pt(model_uri)
        return _MLFLOW_WEIGHTS_CACHE[ck]

    client = mlflow.tracking.MlflowClient()
    if version is not None:
        mv = client.get_model_version(registered_model, str(version))
    elif alias is not None:
        mv = client.get_model_version_by_alias(registered_model, alias)
    elif run_id is not None:
        mv = next(
            (m for m in client.search_model_versions(f"name='{registered_model}'")
             if m.run_id == run_id),
            None,
        )
        if mv is None:
            ck = f"run:{run_id}"
            if ck not in _MLFLOW_WEIGHTS_CACHE:
                local = Path(mlflow.artifacts.download_artifacts(run_id=run_id))
                _MLFLOW_WEIGHTS_CACHE[ck] = _pick_checkpoint(local)
            return _MLFLOW_WEIGHTS_CACHE[ck]
    else:
        raise ValueError("resolve_mlflow_weights: pass model_uri, version, alias, or run_id")

    ck = f"{registered_model}:v{mv.version}"
    if ck not in _MLFLOW_WEIGHTS_CACHE:
        _MLFLOW_WEIGHTS_CACHE[ck] = download_best_pt(mv.source)
    return _MLFLOW_WEIGHTS_CACHE[ck]


def resolve_weights(alias: str, registered_model: str, alias_namespace: str) -> Path:
    """A model alias as a local checkpoint path. lamindb first, MLflow fallback."""
    try:
        return _resolve_lamin_alias(alias, alias_namespace)
    except Exception as lamin_err:
        try:
            return resolve_mlflow_weights(registered_model, alias=alias)
        except Exception as mlflow_err:
            raise RuntimeError(
                f"could not resolve model alias {alias!r} "
                f"(registered_model={registered_model!r}, namespace={alias_namespace!r}).\n"
                f"  lamindb: {type(lamin_err).__name__}: {lamin_err}\n"
                f"  mlflow:  {type(mlflow_err).__name__}: {mlflow_err}\n"
                f"Set it first with clinicalomx.promote(...)."
            ) from mlflow_err


def _artifact_for_run(run_id: str) -> ln.Artifact | None:
    """Latest ``kind='model'`` Artifact whose ``mlflow_run_id`` is ``run_id``, at any version."""
    import lamindb as ln
    for art in ln.Artifact.filter(kind="model").order_by("-created_at"):
        try:
            if (art.features.get_values() or {}).get("mlflow_run_id") == run_id:
                return art
        except Exception:
            continue
    return None


def _mlflow_version_for_run(client, registered_model: str, run_id: str) -> str | None:
    """The MLflow registered-model version whose source run is ``run_id``, or None."""
    try:
        for mv in client.search_model_versions(f"name='{registered_model}'"):
            if mv.run_id == run_id:
                return mv.version
    except Exception:
        return None
    return None


def sync_alias(alias: str, registered_model: str, alias_namespace: str) -> ln.Artifact | None:
    """Mirror the Artifact MLflow's ``alias`` points at into the lamindb alias ULabel.

    Requires MLflow. Returns the mirrored lamindb Artifact.
    """
    import lamindb as ln
    client = _mlflow_client()
    if client is None:
        raise RuntimeError(
            "sync_alias requires MLflow (the source of truth): set MLFLOW_TRACKING_URI and "
            "install clinicalomx[models]."
        )
    mv = client.get_model_version_by_alias(registered_model, alias)
    art = _artifact_for_run(mv.run_id)
    if art is None:
        raise LookupError(
            f"MLflow {alias!r} -> {registered_model} v{mv.version} (run {mv.run_id}) has no "
            f"matching lamindb kind='model' Artifact (was mlflow_run_id stamped at registration?)."
        )
    label = _alias_label(alias, alias_namespace, create=True)
    for prev in ln.Artifact.filter(ulabels=label):
        if prev.uid != art.uid:
            prev.ulabels.remove(label)
    art.ulabels.add(label)
    print(f"lamindb: mirrored {alias!r} -> {art.key} "
          f"(uid={art.uid}, MLflow v{mv.version}, run {mv.run_id})")
    return art


def sync_aliases(registered_model: str, alias_namespace: str) -> dict[str, str]:
    """Mirror every MLflow alias of ``registered_model`` into lamindb, and prune the rest.

    A lamindb alias MLflow no longer has is detached and its ULabel deleted. Returns
    ``{alias: lamindb_uid}``; an alias with no matching lamindb Artifact is skipped.
    """
    import lamindb as ln
    client = _mlflow_client()
    if client is None:
        raise RuntimeError("sync_aliases requires MLflow (the source of truth)")
    rm = client.get_registered_model(registered_model)
    aliases = getattr(rm, "aliases", None) or {}
    out: dict[str, str] = {}
    for a in list(aliases):
        try:
            art = sync_alias(a, registered_model, alias_namespace)
            if art is not None:
                out[a] = art.uid
        except Exception as exc:
            print(f"  skip alias {a!r}: {type(exc).__name__}: {exc}")

    mlflow_names = set(aliases)
    atype = _alias_type(alias_namespace)
    if atype is not None:
        for label in ln.ULabel.filter(type=atype):
            if label.name in mlflow_names:
                continue
            for art in ln.Artifact.filter(ulabels=label):
                art.ulabels.remove(label)
            try:
                label.delete()
            except Exception:
                pass  # best-effort delete of an empty ULabel
            print(f"  pruned lamindb alias {label.name!r} (not in MLflow {registered_model})")
    return out


def promote(
    alias: str,
    registered_model: str,
    alias_namespace: str,
    *,
    run_id: str | None = None,
    lamin_uid: str | None = None,
    lamin_key: str | None = None,
    mlflow_version: str | int | None = None,
) -> ln.Artifact | None:
    """Set ``alias`` in MLflow, then mirror it into lamindb. Requires MLflow.

    The version comes from ``mlflow_version``, else ``run_id``, else the lamindb handle's
    ``mlflow_run_id``. Returns the mirrored lamindb Artifact.
    """
    import lamindb as ln
    client = _mlflow_client()
    if client is None:
        raise RuntimeError(
            "promote requires MLflow (the alias source of truth): set MLFLOW_TRACKING_URI and "
            "install clinicalomx[models]. lamindb subscribes via sync_alias - there is no "
            "lamindb-only promote."
        )
    version = str(mlflow_version) if mlflow_version is not None else None
    if version is None:
        rid = run_id
        if rid is None:
            if not (lamin_uid or lamin_key):
                raise ValueError("pass mlflow_version, run_id, lamin_uid, or lamin_key")
            art = ln.Artifact.get(uid=lamin_uid) if lamin_uid else ln.Artifact.get(key=lamin_key)
            rid = (art.features.get_values() or {}).get("mlflow_run_id")
        if not rid:
            raise LookupError("could not determine a run_id to resolve the MLflow version")
        version = _mlflow_version_for_run(client, registered_model, rid)
        if version is None:
            raise LookupError(f"no MLflow {registered_model} version for run {rid!r}")

    client.set_registered_model_alias(registered_model, alias, version)
    print(f"mlflow:  alias {alias!r} -> {registered_model} v{version} (source of truth)")
    return sync_alias(alias, registered_model, alias_namespace)


def alias_status(alias: str, registered_model: str, alias_namespace: str) -> dict:
    """What ``alias`` resolves to on each registry, and ``in_sync`` when both share a run id."""
    lamin_art = resolve_model_artifact(alias, alias_namespace)
    lamin_run = _features_of(lamin_art).get("mlflow_run_id")
    mlflow_ver = mlflow_run = None
    client = _mlflow_client()
    if client is not None:
        try:
            mv = client.get_model_version_by_alias(registered_model, alias)
            mlflow_ver, mlflow_run = mv.version, mv.run_id
        except Exception:
            pass
    return {
        "alias": alias,
        "lamin": {
            "uid": lamin_art.uid if lamin_art else None,
            "version": lamin_art.version if lamin_art else None,
            "run_id": lamin_run,
        },
        "mlflow": {"version": mlflow_ver, "run_id": mlflow_run},
        "in_sync": bool(lamin_run and mlflow_run and lamin_run == mlflow_run),
    }


def _mlflow_client():
    """An ``MlflowClient`` if ``MLFLOW_TRACKING_URI`` is set and mlflow is installed, else
    ``None``."""
    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        return None
    try:
        mlflow = _mlflow()
    except Exception:
        return None
    mlflow.set_tracking_uri(uri)
    return mlflow.tracking.MlflowClient()


def _features_of(artifact: ln.Artifact | None) -> dict:
    if artifact is None:
        return {}
    try:
        return artifact.features.get_values() or {}
    except Exception:
        return {}


class ModelRef:
    """A handle to one model across lamindb and MLflow. ``.path`` is the local checkpoint."""

    def __init__(
        self,
        artifact: ln.Artifact | None = None,
        *,
        registered_model: str,
        alias_namespace: str | None = None,
        mlflow_coord: dict | None = None,
    ):
        if artifact is None and not mlflow_coord:
            raise ValueError("ModelRef needs a lamindb model Artifact or an mlflow_coord")
        self.artifact = artifact
        self.registered_model = registered_model
        self.alias_namespace = alias_namespace
        self._mlflow_coord = mlflow_coord  # {version|alias|run_id|model_uri}
        self._mlflow_version: Any = _MISSING
        self._weights_path: Path | None = None

    # Constructors.
    @classmethod
    def from_lamindb(
        cls,
        *,
        uid: str | None = None,
        key: str | None = None,
        alias: str | None = None,
        registered_model: str,
        alias_namespace: str | None = None,
    ) -> ModelRef:
        import lamindb as ln
        if uid:
            art = ln.Artifact.get(uid=uid)
        elif key:
            art = ln.Artifact.get(key=key, is_latest=True)
        elif alias:
            if alias_namespace is None:
                raise ValueError("from_lamindb(alias=...) requires alias_namespace")
            art = resolve_model_artifact(alias, alias_namespace)
            if art is None:
                raise LookupError(f"no lamindb model for alias {alias!r} in {alias_namespace!r}")
        else:
            raise ValueError("from_lamindb: pass one of uid / key / alias")
        return cls(art, registered_model=registered_model, alias_namespace=alias_namespace)

    @classmethod
    def from_mlflow(
        cls,
        *,
        run_id: str | None = None,
        version: str | int | None = None,
        alias: str | None = None,
        registered_model: str,
        alias_namespace: str | None = None,
    ) -> ModelRef:
        if version is not None:
            coord = {"version": str(version)}
        elif alias is not None:
            coord = {"alias": alias}
        elif run_id is not None:
            coord = {"run_id": run_id}
        else:
            raise ValueError("from_mlflow: pass one of run_id / version / alias")

        rid = run_id
        if rid is None:
            client = _mlflow_client()
            if client is None:
                raise RuntimeError("MLFLOW_TRACKING_URI not set; cannot resolve from MLflow")
            mv = (
                client.get_model_version(registered_model, str(version))
                if version is not None
                else client.get_model_version_by_alias(registered_model, alias)
            )
            rid = mv.run_id
        art = cls._artifact_for_run(rid)
        return cls(art, registered_model=registered_model,
                   alias_namespace=alias_namespace, mlflow_coord=coord)

    # lamindb side. None when MLflow-only.
    @property
    def uid(self) -> str | None:
        return self.artifact.uid if self.artifact is not None else None

    @property
    def key(self) -> str | None:
        return self.artifact.key if self.artifact is not None else None

    @property
    def version(self) -> str | None:
        return self.artifact.version if self.artifact is not None else None

    @property
    def path(self) -> Path:
        """Local checkpoint: the lamindb file if present, else an MLflow download."""
        if self.artifact is not None:
            p = Path(self.artifact.path)
            if p.exists():
                return p
        return self._mlflow_weights()

    # Lineage.
    def as_artifact(self, *, ensure: bool = False, key: str | None = None,
                    storage=None, force: bool = False) -> ln.Artifact | None:
        """The lamindb ``kind="model"`` Artifact for this ref, or ``None``.

        With ``ensure``, an MLflow-only ref registers its weights at ``key`` (default
        ``models/<registered_model>/<file>``) under ``storage``. Cached on the ref.
        """
        import lamindb as ln
        if self.artifact is not None:
            return self.artifact
        if not ensure:
            return None
        p = self.path
        art_key = key or f"models/{self.registered_model}/{Path(p).name}"
        existing = (
            None if force else ln.Artifact.filter(key=art_key, is_latest=True).one_or_none()
        )
        if existing is not None:
            self.artifact = existing
            return existing
        art = ln.Artifact(
            p, key=art_key, kind="model", storage=storage,
            description=f"Served model {self.registered_model} "
                        f"(mlflow_run_id={self.mlflow_run_id}).",
        ).save()
        coords = {k: v for k, v in {"mlflow_run_id": self.mlflow_run_id,
                                    "model_uri": self.model_uri}.items() if v}
        for name in coords:
            if ln.Feature.filter(name=name).one_or_none() is None:
                ln.Feature(name=name, dtype=str).save()
        if coords:
            art.features.add_values(coords)
        self.artifact = art
        return art

    def consume(self, produced, *, ensure: bool = False, key: str | None = None,
                storage=None) -> ln.Artifact | None:
        """Record this model as a run input of ``produced``, an Artifact or Collection.

        Call after ``produced.save()``. Returns the model Artifact, or ``None`` when skipped.
        """
        model_art = self.as_artifact(ensure=ensure, key=key, storage=storage)
        run = getattr(produced, "run", None)
        if model_art is not None and run is not None:
            run.input_artifacts.add(model_art)
        return model_art

    # MLflow side. Lazy and cached.
    @property
    def mlflow_run_id(self) -> str | None:
        if self.artifact is not None:
            return _features_of(self.artifact).get("mlflow_run_id")
        return (self._mlflow_coord or {}).get("run_id")

    @property
    def mlflow_version(self) -> str | None:
        if self._mlflow_version is _MISSING:
            coord_v = (self._mlflow_coord or {}).get("version")
            self._mlflow_version = coord_v if coord_v else self._resolve_mlflow_version()
        return self._mlflow_version

    @property
    def model_uri(self) -> str | None:
        """``models:/<registered_model>/<version>`` (loadable via mlflow), or None."""
        stamped = _features_of(self.artifact).get("model_uri")
        if stamped:
            return stamped
        v = self.mlflow_version
        return f"models:/{self.registered_model}/{v}" if v else None

    # Internals.
    def _mlflow_weights(self) -> Path:
        if self._weights_path is not None:
            return self._weights_path
        coord = dict(self._mlflow_coord or {})
        if not coord:
            feats = _features_of(self.artifact)
            if feats.get("model_uri"):
                coord = {"model_uri": feats["model_uri"]}
            elif self.mlflow_version:
                coord = {"version": self.mlflow_version}
            elif self.mlflow_run_id:
                coord = {"run_id": self.mlflow_run_id}
            else:
                raise RuntimeError(f"no MLflow coordinate to download weights for {self!r}")
        self._weights_path = resolve_mlflow_weights(self.registered_model, **coord)
        return self._weights_path

    def _resolve_mlflow_version(self) -> str | None:
        rid = self.mlflow_run_id
        if not rid:
            return None
        client = _mlflow_client()
        if client is None:
            return None
        try:
            for mv in client.search_model_versions(f"name='{self.registered_model}'"):
                if mv.run_id == rid:
                    return mv.version
        except Exception:
            return None
        return None

    @staticmethod
    def _artifact_for_run(run_id: str) -> ln.Artifact | None:
        """Latest kind='model' Artifact stamped with this ``mlflow_run_id``."""
        return _artifact_for_run(run_id)

    def __repr__(self) -> str:
        return (
            f"ModelRef(registered_model={self.registered_model!r}, artifact={self.key!r} "
            f"v{self.version} uid={self.uid}, mlflow_run_id={self.mlflow_run_id!r}, "
            f"mlflow_version={self.mlflow_version!r})"
        )
