"""Tests for reading an Artifact-valued feature as a scalar, a set or a list. ``as_artifact`` is
stubbed."""
from __future__ import annotations

import pytest

from clinicalomx import artifacts


class _FakeArtifact:
    def __init__(self, key, created_at):
        self.key = key
        self.created_at = created_at
        self.uid = key


@pytest.fixture(autouse=True)
def _identity_resolver(monkeypatch):
    monkeypatch.setattr(artifacts, "as_artifact", lambda v: v)


@pytest.mark.parametrize(
    "value",
    [
        None,
        _FakeArtifact("a", 1),
        [_FakeArtifact("a", 1), _FakeArtifact("b", 2)],
        (_FakeArtifact("a", 1),),
        {_FakeArtifact("a", 1), _FakeArtifact("b", 2)},
    ],
    ids=["none", "single", "list", "tuple", "set"],
)
def test_newest_artifact_never_raises_on_shape(value):
    got = artifacts.newest_artifact(value)
    assert got is None if value is None else isinstance(got, _FakeArtifact)


def test_newest_artifact_picks_the_latest_from_an_unordered_set():
    old, new = _FakeArtifact("old", 1), _FakeArtifact("new", 2)
    assert artifacts.newest_artifact({old, new}).key == "new"
    assert artifacts.newest_artifact({new, old}).key == "new"


def test_newest_artifact_falls_back_to_the_last_element_without_created_at():
    class _NoTimestamp:
        key = uid = "x"
        created_at = None

    first, last = _NoTimestamp(), _NoTimestamp()
    assert artifacts.newest_artifact([first, last]) is last


def test_all_artifacts_accepts_every_shape():
    a, b = _FakeArtifact("a", 1), _FakeArtifact("b", 2)
    assert artifacts.all_artifacts(None) == []
    assert artifacts.all_artifacts(a) == [a]
    assert artifacts.all_artifacts([a, b]) == [a, b]
    assert set(artifacts.all_artifacts({a, b})) == {a, b}


def test_all_artifacts_drops_unresolvable_values(monkeypatch):
    a = _FakeArtifact("a", 1)
    monkeypatch.setattr(artifacts, "as_artifact", lambda v: v if v is a else None)
    assert artifacts.all_artifacts([a, "gone"]) == [a]


def test_newest_artifact_is_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(artifacts, "as_artifact", lambda _v: None)
    assert artifacts.newest_artifact(["gone", "also-gone"]) is None
