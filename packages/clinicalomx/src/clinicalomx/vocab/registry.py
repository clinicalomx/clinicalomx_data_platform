"""Consumer-owned vocabulary, declared by the repo that uses it.

``data/`` holds only what the framework reads or writes plus shared conventions. A consumer
declares the rest:

    # myrepo/vocab.py
    from clinicalomx.vocab import Vocabulary

    VOCAB = Vocabulary("myrepo")
    BGNORM_ATTEMPTS = VOCAB.feature("bgnorm_attempts", int, "runner launches for this core")
    MY_KIND = VOCAB.type("MyRoiKind", "What it is.", parent="ROI")

    # myrepo/pyproject.toml
    [project.entry-points."clinicalomx.vocabulary"]
    myrepo = "myrepo.vocab:VOCAB"

``VOCAB.ensure()`` creates them, idempotently, at stage startup. The entry point lets ``audit``
name each feature's owner and ``apply --with-consumers`` provision a fresh instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Entry-point group consumers advertise their :class:`Vocabulary` under.
ENTRY_POINT_GROUP = "clinicalomx.vocabulary"

#: Owner recorded for the base in ``data/``.
FOUNDATION = "clinicalomx"


@dataclass(frozen=True)
class Declared:
    """One declared name, with the owner that answers for it."""

    name: str
    dtype: Any
    description: str = ""
    owner: str = ""


@dataclass
class Vocabulary:
    """The features and record types one repo owns. ``owner`` is the distribution name."""

    owner: str
    features: dict[str, Declared] = field(default_factory=dict)
    types: dict[str, Declared] = field(default_factory=dict)
    sources: dict[str, dict] = field(default_factory=dict)

    def feature(self, name: str, dtype: Any, description: str = "") -> str:
        """Declare a feature. Returns ``name``, so it can double as the constant.

        Always give a description: a used feature cannot be deleted.
        """
        if name in self.features and self.features[name].dtype is not dtype:
            raise ValueError(
                f"{self.owner} declares {name!r} twice with different dtypes: "
                f"{self.features[name].dtype} then {dtype}"
            )
        self.features[name] = Declared(name, dtype, description, self.owner)
        return name

    def type(self, name: str, description: str = "", *, parent: str | None = None) -> str:
        """Declare a record type (``is_type=True``). Returns ``name``."""
        self.types[name] = Declared(name, parent, description, self.owner)
        return name

    def source(self, entity: str, *, source: str, version: str,
               organism: str | None = None, relink: bool = False) -> None:
        """Pin a public ontology this repo needs, such as ``bt.Protein`` at a UniProt release.

        ``relink`` moves records held at another release onto this pin, rewriting shared rows.
        """
        from .ontology import validate_pin

        validate_pin(entity, version)
        self.sources[entity] = {"source": source, "version": version, "organism": organism,
                                "relink": relink}

    def ensure(self, *, dry_run: bool = False):
        """Reconcile this vocabulary against the connected instance. Idempotent.

        Returns the :class:`~clinicalomx.vocab.upsert.Reconciler`.
        """
        from .upsert import Reconciler

        rec = Reconciler(dry_run=dry_run)
        resolved: dict[str, Any] = {}
        for decl in self.types.values():
            kwargs = {"description": decl.description}
            if decl.dtype:  # parent type name, stored in .dtype
                kwargs["type"] = resolved.get(decl.dtype) or rec.type(decl.dtype)
            resolved[decl.name] = rec.type(decl.name, **kwargs)
        for decl in self.features.values():
            dtype = resolved.get(decl.dtype, decl.dtype)
            rec.feature(decl.name, dtype, description=decl.description or None)
        if self.sources:
            from .ontology import reconcile

            for entity, spec in self.sources.items():
                reconcile(rec, entity, spec)
        return rec


#: Memoised :func:`discover` result. ``None`` until the first scan.
_DISCOVERED: dict[str, Vocabulary] | None = None


def reset_cache() -> None:
    """Drop the memoised :func:`discover` result. For tests."""
    global _DISCOVERED
    _DISCOVERED = None


def discover() -> dict[str, Vocabulary]:
    """Every :class:`Vocabulary` advertised by an installed distribution. Memoised.

    A consumer that is not installed contributes nothing.
    """
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED

    from importlib.metadata import entry_points

    found: dict[str, Vocabulary] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            vocab = ep.load()
        except Exception as exc:  # report a broken consumer and continue
            print(f"  WARNING  could not load vocabulary {ep.name!r}: {exc}")
            continue
        if isinstance(vocab, Vocabulary):
            found[vocab.owner or ep.name] = vocab
        else:
            print(f"  WARNING  {ep.name!r} is not a Vocabulary: {type(vocab).__name__}")
    _DISCOVERED = found
    return found


def owners() -> dict[str, str]:
    """``{feature name: owner}`` across the base and every installed consumer."""
    from .dataio import features as declared_features

    out = {row["name"].strip(): FOUNDATION for row in declared_features()}
    for owner, vocab in discover().items():
        for name in vocab.features:
            out.setdefault(name, owner)
    return out


def source_owners() -> dict[str, str]:
    """``{bionty entity: owner}`` for every pinned ontology, the base's lockfile included."""
    from .dataio import sources_lock

    out = dict.fromkeys(sources_lock().get("sources") or {}, FOUNDATION)
    for owner, vocab in discover().items():
        for entity in vocab.sources:
            out.setdefault(entity, owner)
    return out
