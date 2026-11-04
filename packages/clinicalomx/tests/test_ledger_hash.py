"""The vocabulary hash changes with the parsed declaration, not with cosmetic edits. No database."""
from __future__ import annotations

from clinicalomx.vocab import ledger


def _hash_with(monkeypatch, **overrides):
    """content_hash() with some dataio readers stubbed out."""
    from clinicalomx.vocab import dataio

    for name, value in overrides.items():
        monkeypatch.setattr(dataio, name, lambda _v=value, **_: _v)
    return ledger.content_hash()


BASE_FEATURES = [
    {"name": "patient_id", "dtype": "str", "description": "x", "coerce": "true",
     "status": "active"},
    {"name": "roi_uid", "dtype": "str", "description": "", "coerce": "", "status": "active"},
]


def test_hash_is_stable_across_calls(monkeypatch):
    first = _hash_with(monkeypatch, features=list(BASE_FEATURES))
    second = _hash_with(monkeypatch, features=list(BASE_FEATURES))
    assert first == second


def test_row_order_does_not_change_the_hash(monkeypatch):
    forward = _hash_with(monkeypatch, features=list(BASE_FEATURES))
    reverse = _hash_with(monkeypatch, features=list(reversed(BASE_FEATURES)))
    assert forward == reverse


def test_surrounding_whitespace_does_not_change_the_hash(monkeypatch):
    padded = [{k: f"  {v}  " if v else v for k, v in row.items()} for row in BASE_FEATURES]
    assert _hash_with(monkeypatch, features=list(BASE_FEATURES)) == _hash_with(
        monkeypatch, features=padded
    )


def test_a_changed_dtype_does_change_the_hash(monkeypatch):
    altered = [dict(row) for row in BASE_FEATURES]
    altered[0]["dtype"] = "int"
    assert _hash_with(monkeypatch, features=list(BASE_FEATURES)) != _hash_with(
        monkeypatch, features=altered
    )


def test_a_changed_storage_root_does_change_the_hash(monkeypatch):
    a = [{"key": "hpc_file", "root": "/scratch/project/exampleproj/dbstorage"}]
    b = [{"key": "hpc_file", "root": "/scratch/project_mnt/S0000/dbstorage"}]
    assert _hash_with(monkeypatch, storages=a) != _hash_with(monkeypatch, storages=b)


def test_dropping_a_feature_from_the_base_changes_the_hash(monkeypatch):
    fewer = BASE_FEATURES[:1]
    assert _hash_with(monkeypatch, features=list(BASE_FEATURES)) != _hash_with(
        monkeypatch, features=fewer
    )
