"""Readers for ``data/``, the declared vocabulary state."""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any

import yaml

DATA = Path(__file__).parent / "data"


def _rows(name: str) -> list[dict[str, str]]:
    path = DATA / name
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        # Leading '#' lines carry provenance.
        body = (line for line in fh if not line.lstrip().startswith("#"))
        return [r for r in csv.DictReader(body)
                if (r.get("name") or r.get("canonical_name") or "").strip()]


def load_yaml(name: str) -> Any:
    path = DATA / name
    return yaml.safe_load(path.read_text()) if path.exists() else None


_SITE_VALUE = re.compile(r"\$\{(\w+)\}")


class UnsetSiteValue(LookupError):
    """A declared value names a site variable that is not set where it is being read."""


def storages(*, expand: bool = True) -> list[dict[str, Any]]:
    """Declared storage roots. Each root is a ``${NAME}`` site value from site.env.

    ``expand=True`` substitutes the values and raises on an unset one. ``expand=False`` returns
    the declaration as written.
    """
    rows = (load_yaml("storages.yaml") or {}).get("storages", [])
    if not expand:
        return rows
    return [{**row, "root": _expand(row["root"], row["key"])} for row in rows]


def _expand(value: str, key: str) -> str:
    def one(match: re.Match) -> str:
        if not os.environ.get(match.group(1)):
            raise UnsetSiteValue(
                f"storages.yaml {key}: {match.group(1)} is not set. It is a site value: render "
                "site.env with scripts/site/render.py and export it before touching an instance."
            )
        return os.environ[match.group(1)]

    return _SITE_VALUE.sub(one, value)


def types() -> list[dict[str, Any]]:
    return (load_yaml("types.yaml") or {}).get("types", [])


def features() -> list[dict[str, str]]:
    """Declared features. ``status='deprecated'`` rows are kept but not seeded."""
    return _rows("features.csv")


def sources_lock() -> dict[str, Any]:
    return load_yaml("sources.lock.yaml") or {}


# Dtypes.
#: How ``features.csv`` spells a dtype. ``type:<Name>`` resolves at apply time against the type
#: records seeded in s0002; the file stores the name, never a UID.
_SCALARS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "datetime": "datetime",
    "date": "date",
    "num": "num",
}

#: lamindb's serialised registry dtype, such as ``cat[bionty.Protein]``. A dotted name selects the
#: module.
_CAT_REGISTRY = re.compile(r"^cat\[([\w.]+)\]$")

#: ``cat[Record[<uid>]]``: a categorical pinned to a type UID that no longer resolves. See
#: :class:`LiteralDtype`.
_CAT_RECORD_UID = re.compile(r"^cat\[Record\[[^\]]+\]\]$")


class LiteralDtype(str):
    """A dtype compared as a string and never written: a dangling categorical pin."""

    __slots__ = ()


def resolve_dtype(spec: str, type_records: dict[str, Any]) -> Any:
    """``features.csv`` dtype string -> something ``ln.Feature(dtype=...)`` accepts."""
    import lamindb as ln

    spec = (spec or "").strip()
    if spec in _SCALARS:
        return _SCALARS[spec]
    if spec.startswith("type:"):
        name = spec.split(":", 1)[1]
        if name not in type_records:
            raise KeyError(
                f"features.csv dtype {spec!r} references type {name!r}, which is not "
                "declared in types.yaml"
            )
        return type_records[name]
    if _CAT_RECORD_UID.match(spec):
        return LiteralDtype(spec)

    # The bare registry name, or lamindb's serialised cat[...] form.
    m = _CAT_REGISTRY.match(spec)
    registry = m.group(1) if m else spec
    obj = _registry(ln, registry)
    if isinstance(obj, type):
        return obj

    # cat[ULabel[<subtype>]] and anything else: lamindb parses it.
    return spec


def _registry(ln, name: str) -> Any:
    """``Artifact`` -> ``ln.Artifact``; ``bionty.Protein`` -> ``bionty.Protein``."""
    if "." not in name:
        return getattr(ln, name, None)
    module, _, attr = name.partition(".")
    try:
        mod = __import__(module)
    except ImportError:
        return None
    return getattr(mod, attr, None)
