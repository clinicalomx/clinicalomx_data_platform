"""Model registry access across MLflow and lamindb.

``framework_of`` reads which framework trained a registered model. ``Register`` writes an alias
on both registries. ``ChampionRegistry`` resolves one objective's champion to a checkpoint, an
Artifact or a loaded detector. This module is an inference entry point and must never import
mlflow, not even inside a function.
"""
from __future__ import annotations

import abc
import typing as T
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import clinicalomx
import pydantic as pdt

if TYPE_CHECKING:
    from croi.detection.detector import ObjectDetector

DEFAULT_ALIAS = clinicalomx.DEFAULT_ALIAS


BACKFILL = "croi.utils.backfill_model_framework"


class UnrecordedFramework(LookupError):
    """A registered model carries no ``model_framework`` feature, so no backend can be chosen."""


def framework_of(artifact) -> str:
    """The framework recorded in the model's ``model_framework`` feature.

    Raises `UnrecordedFramework` when absent; never defaults and never reads the file suffix.
    """
    from croi import feature_names

    values = artifact.features.get_values() or {}
    framework = values.get(feature_names.MODEL_FRAMEWORK)
    if not framework:
        raise UnrecordedFramework(
            f"model {artifact.key} (uid={artifact.uid}) records no "
            f"{feature_names.MODEL_FRAMEWORK}, so its backend is unknown; stamp it with "
            f"`python -m {BACKFILL} apply {artifact.uid}=<ultralytics|rfdetr>`"
        )
    return str(framework)


class Register(abc.ABC, pdt.BaseModel, strict=True, frozen=True, extra="forbid"):
    """Point an alias at an already-registered model. The only write in this module."""

    KIND: str

    registered_model: str
    """MLflow registered-model name, e.g. ``roi-core``."""

    alias_namespace: str
    """lamindb alias ULabel type, e.g. ``model_alias_core``."""

    @abc.abstractmethod
    def register(self, alias: str = DEFAULT_ALIAS, *, lamin_uid: str | None = None,
                 lamin_key: str | None = None, mlflow_version: str | int | None = None):
        """Point ``alias`` at the named model and return what the registry recorded.

        Exactly one of ``lamin_uid``, ``lamin_key`` or ``mlflow_version`` selects the model.
        """


class LamindbMlflowRegister(Register):
    """Promote on both lamindb and MLflow, or neither."""

    KIND: T.Literal["LamindbMlflowRegister"] = "LamindbMlflowRegister"

    def register(self, alias: str = DEFAULT_ALIAS, *, lamin_uid: str | None = None,
                 lamin_key: str | None = None, mlflow_version: str | int | None = None):
        return clinicalomx.promote(
            alias, self.registered_model, self.alias_namespace,
            lamin_uid=lamin_uid, lamin_key=lamin_key, mlflow_version=mlflow_version,
        )


RegisterKind = LamindbMlflowRegister
"""Every ``Register`` a config may name."""


@dataclass(frozen=True)
class ChampionRegistry:
    """One objective's champion line: where its models are registered, and how aliased."""

    registered_model: str   # MLflow registered-model name, e.g. ``roi-core``
    alias_namespace: str    # lamindb alias ULabel type, e.g. ``model_alias_core``

    @property
    def register(self) -> RegisterKind:
        """The ``Register`` that writes this line's aliases."""
        return LamindbMlflowRegister(
            registered_model=self.registered_model, alias_namespace=self.alias_namespace,
        )

    def model_ref(self, sel: dict | None = None, /, **selector: Any) -> clinicalomx.ModelRef:
        """Build a :class:`clinicalomx.ModelRef` from an ``inputs.model`` selector.

        Accepts lamindb (``uid`` / ``key`` / ``alias``) or MLflow (``mlflow_version`` /
        ``mlflow_alias`` / ``mlflow_run_id``) keys. Defaults to the lamindb ``production`` alias.
        """
        s = {**(sel or {}), **selector}
        if any(s.get(k) for k in ("mlflow_run_id", "mlflow_version", "mlflow_alias", "run_id",
                                  "version")):
            return clinicalomx.ModelRef.from_mlflow(
                run_id=s.get("mlflow_run_id") or s.get("run_id"),
                version=s.get("mlflow_version") or s.get("version"),
                alias=s.get("mlflow_alias"),
                registered_model=self.registered_model, alias_namespace=self.alias_namespace,
            )
        return clinicalomx.ModelRef.from_lamindb(
            uid=s.get("uid"), key=s.get("key"), alias=s.get("alias") or DEFAULT_ALIAS,
            registered_model=self.registered_model, alias_namespace=self.alias_namespace,
        )

    def resolve_weights(self, alias: str = DEFAULT_ALIAS) -> Path:
        """Local best.pt for ``alias``: lamindb first, MLflow fallback."""
        return clinicalomx.resolve_weights(alias, self.registered_model, self.alias_namespace)

    def resolve_model_artifact(self, alias: str = DEFAULT_ALIAS):
        """The lamindb model Artifact ``alias`` points at, or None."""
        return clinicalomx.resolve_model_artifact(alias, self.alias_namespace)

    def load_champion(self, alias: str = DEFAULT_ALIAS, cfg: dict | None = None) -> ObjectDetector:
        """Load the aliased champion as an ``ObjectDetector``. The production entry point.

        The framework is the one recorded on the model. ``cfg`` is the ``model:`` block; a
        ``framework`` in it that contradicts the recorded one raises, as does an alias with no
        lamindb model. ``weights`` is always the resolved alias. The framework object is
        ``.model`` on the result.
        """
        from croi.detection.backends import detector_for

        cfg = dict(cfg or {})
        artifact = self.resolve_model_artifact(alias)
        if artifact is None:
            raise UnrecordedFramework(
                f"{self.registered_model} alias {alias!r} resolves to no lamindb model, so no "
                f"model records its framework; an alias that exists only in MLflow is refused "
                f"for the same reason"
            )
        recorded = framework_of(artifact)
        asked = cfg.get("framework")
        if asked is not None and asked != recorded:
            raise ValueError(
                f"{self.registered_model} alias {alias!r} is a {recorded} model; "
                f"the config asks for {asked!r}"
            )
        cfg["framework"] = recorded
        return detector_for({**cfg, "weights": self.resolve_weights(alias)},
                            resolve_alias=self.resolve_weights)

    def promote(self, alias: str = DEFAULT_ALIAS, *, lamin_uid: str | None = None,
                lamin_key: str | None = None, mlflow_version: str | int | None = None):
        """Point ``alias`` at a model on both registries (see :class:`Register`)."""
        return self.register.register(
            alias, lamin_uid=lamin_uid, lamin_key=lamin_key, mlflow_version=mlflow_version,
        )

    def resolve_champion(self, selector: str | None = None):
        """Resolve a registered champion for re-evaluation, without training.

        ``selector`` is ``uid:<uid>``, ``version:<n>`` (MLflow version) or a bare uid; None means
        the MLflow ``production`` alias. MLflow is the source of truth for versions and aliases.
        """
        import lamindb as ln
        from clinicalomx import ModelRef

        if selector:
            if selector.startswith("uid:"):
                return ln.Artifact.get(uid=selector[4:])
            if selector.startswith("version:"):
                art = ModelRef.from_mlflow(
                    version=selector[len("version:"):], registered_model=self.registered_model,
                ).as_artifact()
                if art is None:
                    raise SystemExit(
                        f"no lamindb model for {self.registered_model} MLflow version {selector}")
                return art
            return ln.Artifact.get(uid=selector)
        art = ModelRef.from_mlflow(
            alias=DEFAULT_ALIAS, registered_model=self.registered_model,
            alias_namespace=self.alias_namespace,
        ).as_artifact()
        if art is None:
            raise SystemExit(
                f"no lamindb model for {self.registered_model} MLflow '{DEFAULT_ALIAS}' alias; "
                f"train + promote one, or run clinicalomx.sync_aliases first."
            )
        return art
