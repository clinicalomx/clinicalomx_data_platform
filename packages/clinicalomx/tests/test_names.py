"""Vocabulary name tests: no collisions and no near-duplicates. No database."""

from __future__ import annotations

import pytest

from clinicalomx.names import ALL_FEATURES, ALL_TYPES, TYPES
from clinicalomx.vocab.dataio import features, sources_lock, storages, types


def _key(name: str) -> str:
    """Separator- and case-insensitive match key."""
    return name.lower().replace("-", "").replace("_", "").replace(" ", "")


def test_no_duplicate_constants():
    assert len(ALL_FEATURES) == len(set(ALL_FEATURES))
    assert len(ALL_TYPES) == len(set(ALL_TYPES))


def test_no_near_duplicate_features():
    seen: dict[str, str] = {}
    for name in ALL_FEATURES:
        k = _key(name)
        assert k not in seen, f"near-duplicate features: {seen[k]!r} vs {name!r}"
        seen[k] = name


def test_declared_features_cover_the_constants():
    declared = {r["name"].strip() for r in features()}
    if not declared:
        pytest.skip("features.csv is empty - run scripts/dump_instance.py --write first")
    missing = sorted(set(ALL_FEATURES) - declared)
    assert not missing, f"names.py references undeclared features: {missing}"


def test_declared_types_cover_the_constants():
    declared = {t["name"] for t in types()}
    if not declared:
        pytest.skip("types.yaml is empty - run scripts/dump_instance.py --write first")
    runtime = {TYPES.SEED_VERSION}
    missing = sorted(set(ALL_TYPES) - declared - runtime)
    assert not missing, f"names.py references types absent from types.yaml: {missing}"


def test_storages_have_unique_keys_and_roots():
    entries = storages(expand=False)
    assert entries, "storages.yaml is empty"
    assert len({e["key"] for e in entries}) == len(entries)
    assert len({e["root"] for e in entries}) == len(entries)


def test_sources_are_pinned():
    for entity, spec in (sources_lock().get("sources") or {}).items():
        version = str(spec.get("version", ""))
        assert version and version != "latest-version", f"{entity} is not pinned"



