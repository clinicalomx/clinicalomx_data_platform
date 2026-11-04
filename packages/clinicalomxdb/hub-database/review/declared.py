"""The declared vocabulary, read from the clinicalomx package's data files.

Reads features.csv, types.yaml and storages.yaml; never imports the package. Returns plain
dicts.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any

import yaml

FEATURES = "features.csv"
TYPES = "types.yaml"
STORAGES = "storages.yaml"

# Where a data directory may live, most specific first.
_CANDIDATES = ("vocab/data", "data")


class NotFound(Exception):
    """The declaration could not be located. The message names every path tried."""


def find_data_dir(package: str = "clinicalomx") -> Path:
    """The installed package's data directory. Raises NotFound rather than return nothing."""
    import importlib.util

    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise NotFound(
            f"{package} is not installed in this interpreter. Run this inside the "
            "python-lamindb container, or pass --declared with a path to the package's "
            "data directory.")

    # A candidate must hold all three files. An incomplete one is skipped; NotFound names what
    # each lacked. A directory named with --declared bypasses this check.
    tried = []
    for root in spec.submodule_search_locations:
        for candidate in _CANDIDATES:
            path = Path(root) / candidate
            missing = [name for name in (FEATURES, TYPES, STORAGES)
                       if not (path / name).is_file()]
            if not missing:
                return path
            tried.append(f"{path} (no {', '.join(missing)})")
    raise NotFound("no complete declaration under any of: " + "; ".join(tried))


def _rows(path: Path) -> list[dict[str, str]]:
    """CSV rows, minus the leading hash-line provenance header and rows with no name.

    Raises when no named row is found or the dtype column is absent. A blank dtype cell is
    allowed: see compare.normalise_declared_dtype.
    """
    if not path.is_file():
        raise NotFound(f"no {path.name} at {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        body = (line for line in fh if not line.lstrip().startswith("#"))
        reader = csv.DictReader(body)
        rows = [r for r in reader if (r.get("name") or "").strip()]
        fields = list(reader.fieldnames or [])
        columns = ", ".join(fields) or "none"
    if not rows:
        raise NotFound(f"{path} declared no named feature; its columns are: {columns}")
    if "dtype" not in fields:
        raise NotFound(f"{path} has no dtype column, so no declared dtype could be compared "
                       f"against the instance; its columns are: {columns}")
    return rows


def _site_value(root: str, key: str) -> str:
    """Substitute the ${NAME} site values in a declared root. An unset value raises."""
    def one(match: re.Match) -> str:
        if not os.environ.get(match.group(1)):
            raise SystemExit(f"declared storage {key} names {match.group(1)}, which is not set: "
                             "render site.env with scripts/site/render.py and run through make")
        return os.environ[match.group(1)]

    return re.sub(r"\$\{(\w+)\}", one, root)


def _yaml(path: Path, key: str) -> list[dict[str, Any]]:
    """The list under one top-level key. An absent file declares nothing; a missing key raises."""
    if not path.is_file():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if key not in loaded:
        keys = ", ".join(str(k) for k in loaded) or "none"
        raise NotFound(f"{path} has no top-level {key}; its keys are: {keys}")
    return loaded.get(key) or []


def load(data_dir: Path, version: str | None = None) -> dict:
    """The declared state as a snapshot, in file order. Raises NotFound rather than return
    nothing."""
    features = []
    for row in _rows(data_dir / FEATURES):
        features.append({
            "name": (row.get("name") or "").strip(),
            "dtype": (row.get("dtype") or "").strip(),
            "description": (row.get("description") or "").strip(),
            # A blank status means active.
            "status": (row.get("status") or "active").strip() or "active",
        })

    types = []
    for row in _yaml(data_dir / TYPES, "types"):
        types.append({
            "name": str(row.get("name") or "").strip(),
            # Carried so the diff can report a type deleted and recreated.
            "uid": str(row.get("uid") or "").strip(),
            "parent": str(row.get("type") or "").strip(),
            "description": str(row.get("description") or "").strip(),
        })

    storages = []
    for row in _yaml(data_dir / STORAGES, "storages"):
        key = str(row.get("key") or row.get("name") or "").strip()
        storages.append({
            "key": key,
            "root": _site_value(str(row.get("root") or row.get("path") or "").strip(), key),
            "description": str(row.get("description") or "").strip(),
        })

    return {
        "source": str(data_dir),
        "version": version or "",
        "features": features,
        "types": types,
        "storages": storages,
    }


def package_version(package: str = "clinicalomx") -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return ""
