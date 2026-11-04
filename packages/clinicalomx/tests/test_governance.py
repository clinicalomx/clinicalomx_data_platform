"""Tests for governance: the flag ladder, deny-by-default, and consumer declaration."""
from __future__ import annotations

import pytest

from clinicalomx import governance as gov


@pytest.fixture(autouse=True)
def _clear_cache():
    gov.reset_cache(registrations=True)
    yield
    gov.reset_cache(registrations=True)


def _policy(**kw):
    base = {"folder": "f", "readable": True, "ingest": True, "training": False, "derive": False}
    return gov.Policy(**{**base, **kw})


# The implication chain.
def test_ingest_requires_readable():
    with pytest.raises(ValueError, match="ingest requires readable"):
        _policy(readable=False, ingest=True)


def test_deriving_requires_ingest():
    with pytest.raises(ValueError, match="derive requires ingest"):
        _policy(readable=True, ingest=False, derive=True)


def test_training_requires_derive():
    with pytest.raises(ValueError, match="training requires derive"):
        _policy(readable=True, ingest=True, derive=False, training=True)


def test_deriving_without_training_is_the_ordinary_collaborator_case():
    p = _policy(readable=True, ingest=True, derive=True, training=False)
    assert p.derive and not p.training


@pytest.mark.parametrize(
    "kw",
    [
        {"readable": False, "ingest": False},
        {"readable": True, "ingest": False},
        {"readable": True, "ingest": True},
        {"readable": True, "ingest": True, "derive": True},
        {"readable": True, "ingest": True, "training": True, "derive": True},
    ],
    ids=["nothing", "inspect-only", "catalogue", "derive", "everything"],
)
def test_coherent_combinations_are_accepted(kw):
    assert _policy(**kw).folder == "f"


def test_inspect_without_catalogue_is_expressible():
    p = _policy(readable=True, ingest=False)
    assert p.readable and not (p.ingest or p.training or p.derive)


# Deny by default.
def test_undeclared_permits_nothing():
    assert gov.UNDECLARED.permits_nothing


def test_an_undeclared_folder_resolves_to_undeclared(monkeypatch):
    monkeypatch.setattr(gov, "discover", lambda: {"known": _policy(folder="known")})
    assert gov.policy_for("brand_new_dropbox/scan.qptiff") is gov.UNDECLARED
    assert not gov.permits_read("brand_new_dropbox/scan.qptiff")
    assert not gov.permits_ingest("brand_new_dropbox/scan.qptiff")
    assert not gov.permits_training("brand_new_dropbox/scan.qptiff")
    assert not gov.permits_derive("brand_new_dropbox/scan.qptiff")


def test_no_declarations_at_all_still_denies(monkeypatch):
    monkeypatch.setattr(gov, "discover", dict)
    assert gov.policy_for("anything/x") is gov.UNDECLARED


@pytest.mark.parametrize(
    ("key", "expected"),
    [("projects/a/b.qptiff", "projects"), ("projects", "projects"), ("", ""), (None, "")],
    ids=["nested", "bare", "empty", "none"],
)
def test_folder_of(key, expected):
    assert gov.folder_of(key) == expected


@pytest.mark.parametrize(
    ("key", "hidden"),
    [(".trash/x.qptiff", True), ("projects/.hidden/x", True), ("projects/a/x.qptiff", False)],
)
def test_hidden_paths_are_not_governed_data(key, hidden):
    assert gov.is_hidden(key) is hidden


# Consumer declaration, via the entry point.
class _EP:
    def __init__(self, name, value):
        self.name, self._value = name, value

    def load(self):
        return self._value


def _with_entry_points(monkeypatch, eps):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: eps)


def test_discover_reads_a_declared_tuple(monkeypatch):
    _with_entry_points(monkeypatch, [_EP("repo", (_policy(folder="projects"),))])
    assert gov.declared_folders() == ("projects",)
    assert gov.permits_ingest("projects/a.qptiff")


def test_discover_accepts_a_callable(monkeypatch):
    _with_entry_points(monkeypatch, [_EP("repo", lambda: (_policy(folder="projects"),))])
    assert gov.declared_folders() == ("projects",)


def test_two_consumers_claiming_one_folder_differently_raises(monkeypatch):
    _with_entry_points(monkeypatch, [
        _EP("repo-a", (_policy(folder="shared", training=True, derive=True),)),
        _EP("repo-b", (_policy(folder="shared", training=False),)),
    ])
    with pytest.raises(ValueError, match="conflicting policies"):
        gov.declared_folders()


def test_identical_declarations_from_two_consumers_are_fine(monkeypatch):
    _with_entry_points(monkeypatch, [
        _EP("repo-a", (_policy(folder="shared"),)),
        _EP("repo-b", (_policy(folder="shared"),)),
    ])
    assert gov.declared_folders() == ("shared",)


def test_the_table_is_memoised(monkeypatch):
    calls = []

    def _eps(group):
        calls.append(group)
        return [_EP("repo", (_policy(folder="projects"),))]

    _with_entry_points(monkeypatch, None)
    monkeypatch.setattr("importlib.metadata.entry_points", _eps)
    for _ in range(5):
        gov.policy_for("projects/a.qptiff")
    assert len(calls) == 1


# Register_policies: declaring from a consumer that is not pip-installed.
# A consumer that is not pip-installed advertises no entry point; it registers at runtime instead.
def test_a_registered_policy_resolves():
    gov.register_policies((_policy(folder="projects", readable=True, ingest=True),))
    assert gov.permits_read("projects/cohort/slide.qptiff") is True
    assert gov.declared_folders() == ("projects",)


def test_without_registration_the_same_key_is_denied():
    assert gov.permits_read("projects/cohort/slide.qptiff") is False
    assert gov.policy_for("projects/cohort/slide.qptiff") is gov.UNDECLARED


def test_registering_the_same_terms_twice_is_a_no_op():
    p = _policy(folder="projects")
    gov.register_policies((p,))
    gov.register_policies((p,))
    assert gov.declared_folders() == ("projects",)


def test_registering_conflicting_terms_raises():
    gov.register_policies((_policy(folder="projects", derive=True, training=True),))
    with pytest.raises(ValueError, match="different terms"):
        gov.register_policies((_policy(folder="projects", derive=True, training=False),))


def test_registration_after_a_resolution_is_still_seen():
    assert gov.permits_read("projects/x.qptiff") is False   # caches the empty table
    gov.register_policies((_policy(folder="projects", readable=True),))
    assert gov.permits_read("projects/x.qptiff") is True


def test_a_cache_drop_does_not_forget_registrations():
    gov.register_policies((_policy(folder="projects", readable=True),))
    gov.reset_cache()
    assert gov.permits_read("projects/x.qptiff") is True


def test_registrations_can_be_cleared_explicitly_for_tests():
    gov.register_policies((_policy(folder="projects", readable=True),))
    gov.reset_cache(registrations=True)
    assert gov.permits_read("projects/x.qptiff") is False


def test_register_policies_is_reachable_from_the_package_root():
    import clinicalomx as cx

    assert cx.register_policies is gov.register_policies
