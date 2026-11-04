"""materialize(): skip `compute` if every key exists, and always run `reconcile`. No database."""
import clinicalomx
from clinicalomx import idempotency


def _patch_existing(monkeypatch, present: dict):
    """Make existing_artifact return present[key] (None means missing)."""
    monkeypatch.setattr(idempotency, "existing_artifact",
                        lambda key, **kw: present.get(key))


def test_all_exist_skips_compute_but_reconciles(monkeypatch):
    _patch_existing(monkeypatch, {"a": "ART_A", "b": "ART_B"})
    calls = {"compute": 0, "reconcile": None}

    def compute():
        calls["compute"] += 1
        return {"a": "NEW_A", "b": "NEW_B"}

    out = clinicalomx.materialize(
        ["a", "b"], compute=compute,
        reconcile=lambda arts: calls.__setitem__("reconcile", arts),
    )
    assert calls["compute"] == 0                      # skipped
    assert out == {"a": "ART_A", "b": "ART_B"}        # the existing ones
    assert calls["reconcile"] == out                  # reconcile still ran


def test_any_missing_triggers_compute_then_reconcile(monkeypatch):
    _patch_existing(monkeypatch, {"a": "ART_A", "b": None})  # b missing
    calls = {"compute": 0, "reconcile": None}

    def compute():
        calls["compute"] += 1
        return {"a": "NEW_A", "b": "NEW_B"}

    out = clinicalomx.materialize(
        ["a", "b"], compute=compute,
        reconcile=lambda arts: calls.__setitem__("reconcile", arts),
    )
    assert calls["compute"] == 1                       # recomputed
    assert out == {"a": "NEW_A", "b": "NEW_B"}
    assert calls["reconcile"] == out                   # reconcile ran on the new set


def test_reconcile_optional(monkeypatch):
    _patch_existing(monkeypatch, {"a": "ART_A"})
    assert clinicalomx.materialize(["a"], compute=lambda: {"a": "X"}) == {"a": "ART_A"}