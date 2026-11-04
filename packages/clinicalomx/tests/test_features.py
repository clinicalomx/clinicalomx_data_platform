"""Tests for `upsert_values`: it touches only the given keys, and restores them when a write fails.

Also covers `environments._stamp` and `review.set_state`, which delegate to it. No database.
"""
from __future__ import annotations

import sys
import types

import pytest

from clinicalomx.features import upsert_values


class FakeManager:
    """Minimal stand-in for `record.features`."""

    def __init__(self, values: dict | None = None):
        self.store = dict(values or {})
        self.removed: list[list[str]] = []

    def get_values(self) -> dict:
        return dict(self.store)

    def remove_values(self, keys) -> None:
        keys = [keys] if isinstance(keys, str) else list(keys)
        self.removed.append(keys)
        for key in keys:
            self.store.pop(key, None)

    def add_values(self, values: dict) -> None:
        for key, value in values.items():
            if key in self.store:
                raise AssertionError(f"add_values on existing key {key!r} (unique constraint)")
            self.store[key] = value


class RejectingManager(FakeManager):
    """Refuses a value of the wrong type before writing anything, as lamindb's add_values does."""

    def __init__(self, values: dict | None = None, dtypes: dict | None = None):
        super().__init__(values)
        self.dtypes = dict(dtypes or {})

    def add_values(self, values: dict) -> None:
        for key, value in values.items():
            expected = self.dtypes.get(key)
            if expected is not None and not isinstance(value, expected):
                raise ValueError(f"dtype mismatch for {key!r}: expected {expected.__name__}")
        super().add_values(values)


class LateFailingManager(FakeManager):
    """Writes each key as it goes and fails on the first bad value."""

    def add_values(self, values: dict) -> None:
        for key, value in values.items():
            if not isinstance(value, int):
                raise ValueError(f"write failed at {key!r}")
            super().add_values({key: value})


class RefusingRemovalManager(FakeManager):
    """remove_values deletes the keys it reaches, then refuses one, as on an Artifact host."""

    def __init__(self, values: dict | None = None, refuse: str | None = None):
        super().__init__(values)
        self.refuse = refuse

    def remove_values(self, keys) -> None:
        keys = [keys] if isinstance(keys, str) else list(keys)
        self.removed.append(keys)
        for key in keys:
            if key == self.refuse:
                raise ValueError(f"cannot remove values for {key!r}")
            self.store.pop(key, None)


class UnremovableAfterPartialWriteManager(LateFailingManager):
    """The key that landed before the failure cannot be removed during rollback."""

    def __init__(self, values: dict | None = None, unremovable: str | None = None):
        super().__init__(values)
        self.unremovable = unremovable
        self.written = False

    def add_values(self, values: dict) -> None:
        self.written = True
        super().add_values(values)

    def remove_values(self, keys) -> None:
        keys = [keys] if isinstance(keys, str) else list(keys)
        self.removed.append(keys)
        if self.written and self.unremovable in keys:
            raise ValueError(f"lock timeout removing {self.unremovable!r}")
        for key in keys:
            self.store.pop(key, None)


class InterruptedRestoreManager(RejectingManager):
    """The write is refused, then the rollback's add_values is interrupted."""

    def __init__(self, values: dict | None = None, dtypes: dict | None = None):
        super().__init__(values, dtypes)
        self.refused = False

    def add_values(self, values: dict) -> None:
        if self.refused:
            raise KeyboardInterrupt
        try:
            super().add_values(values)
        except ValueError:
            self.refused = True
            raise


class UnreadableAfterFailureManager(RejectingManager):
    """The rollback's own get_values raises, as after a failed write inside atomic()."""

    def __init__(self, values: dict | None = None, dtypes: dict | None = None):
        super().__init__(values, dtypes)
        self.reads = 0

    def get_values(self) -> dict:
        self.reads += 1
        if self.reads > 1:
            raise RuntimeError("connection unusable after the failed write")
        return super().get_values()


class InterruptedManager(FakeManager):
    """add_values is interrupted once (KeyboardInterrupt); the rollback's add_values succeeds."""

    def __init__(self, values: dict | None = None):
        super().__init__(values)
        self.interrupted = False

    def add_values(self, values: dict) -> None:
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        super().add_values(values)


class PartlyWrittenThenUnreadableManager(LateFailingManager):
    """The first key lands, the second fails, and the rollback cannot read the record back."""

    def __init__(self, values: dict | None = None):
        super().__init__(values)
        self.reads = 0

    def get_values(self) -> dict:
        self.reads += 1
        if self.reads > 1:
            raise RuntimeError("connection unusable after the failed write")
        return super().get_values()


class RecordRef:
    """A Record passed as an object, which resolves even when its name is ambiguous."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<Record {self.name!r}>"


class AmbiguousRestoreManager(FakeManager):
    """Refuses the write, then refuses the restore of a relational key whose name is ambiguous."""

    UNREGISTERED = "d1_not_a_feature"

    def __init__(self, values: dict | None = None, ambiguous: str | None = None):
        super().__init__(values)
        self.ambiguous = ambiguous

    def add_values(self, values: dict) -> None:
        for key, value in values.items():
            if key == self.UNREGISTERED:
                raise ValueError(f"These keys could not be validated: [{self.UNREGISTERED!r}]")
            if key == self.ambiguous and isinstance(value, str):
                raise ValueError(f"Ambiguous match for Record {value!r}: found 2 records")
        super().add_values(values)


class Anchored:
    """Anything with ``.features``: a Record or an Artifact."""

    def __init__(self, manager: FakeManager):
        self.features = manager


class AlreadyDeclaredFeature:
    """Stand-in for the `ln.Feature` lookup. Every name is already declared."""

    @classmethod
    def filter(cls, **kwargs):
        return cls()

    def one_or_none(self):
        return self


@pytest.fixture
def _feature_registry(monkeypatch):
    """Satisfy `import lamindb` in the Feature get-or-create paths, without an instance."""
    fake = types.SimpleNamespace(Feature=AlreadyDeclaredFeature)
    monkeypatch.setitem(sys.modules, "lamindb", fake)


def test_unrelated_features_survive():
    m = FakeManager({"marker_state": "proposed", "role_source": "llm"})
    upsert_values(m, {"segmentation_role": "marker"})
    assert m.store == {
        "marker_state": "proposed",
        "role_source": "llm",
        "segmentation_role": "marker",
    }


def test_existing_key_is_replaced_not_duplicated():
    m = FakeManager({"segmentation_role": "nucleus"})
    upsert_values(m, {"segmentation_role": "cyto_structural"})
    assert m.store == {"segmentation_role": "cyto_structural"}
    assert m.removed == [["segmentation_role"]]


def test_only_present_keys_are_removed():
    m = FakeManager({"a": 1})
    upsert_values(m, {"a": 2, "b": 3})
    assert m.removed == [["a"]]
    assert m.store == {"a": 2, "b": 3}


def test_empty_is_a_noop():
    m = FakeManager({"a": 1})
    upsert_values(m, {})
    assert m.store == {"a": 1}
    assert m.removed == []


def test_rejected_write_leaves_every_previous_value_intact():
    m = RejectingManager({"q_age": 40, "q_patient_id": "P1"}, dtypes={"q_age": int})
    with pytest.raises(ValueError):
        upsert_values(m, {"q_age": "forty"})
    assert m.store == {"q_age": 40, "q_patient_id": "P1"}


def test_write_that_fails_part_way_is_rolled_back():
    m = LateFailingManager({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        upsert_values(m, {"a": 10, "b": "twenty"})
    assert m.store == {"a": 1, "b": 2}


def test_removal_that_fails_part_way_is_rolled_back():
    m = RefusingRemovalManager({"a": 1, "b": 2, "c": 3}, refuse="b")
    with pytest.raises(ValueError):
        upsert_values(m, {"a": 10, "b": 20})
    assert m.store == {"a": 1, "b": 2, "c": 3}
    # One removal call: the rollback did not retry the failed one.
    assert m.removed == [["a", "b"]]


def test_an_interrupted_write_leaves_every_previous_value_intact():
    m = InterruptedManager({"q_age": 40, "q_patient_id": "P1"})
    with pytest.raises(KeyboardInterrupt):
        upsert_values(m, {"q_age": 41})
    assert m.store == {"q_age": 40, "q_patient_id": "P1"}


def test_a_rollback_names_what_it_restored_on_the_exception():
    m = RejectingManager({"q_age": 40, "q_patient_id": "P1"}, dtypes={"q_age": int})
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"q_age": "forty"})
    notes = getattr(caught.value, "__notes__", [])
    assert any("q_age" in note and "40" in note for note in notes), notes
    assert type(caught.value) is ValueError
    assert m.store == {"q_age": 40, "q_patient_id": "P1"}


def test_a_set_valued_capture_is_refused_rather_than_replayed():
    m = RejectingManager(
        {"roi_uid": {"a", "b"}, "def_text": "Bootstrap: docker\n"},
        dtypes={"def_text": str},
    )
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"roi_uid": "c", "def_text": 17})
    assert m.store == {"def_text": "Bootstrap: docker\n"}
    notes = getattr(caught.value, "__notes__", [])
    refused = [note for note in notes if "roi_uid" in note and "def_text" not in note]
    restored = [note for note in notes if "def_text" in note and "roi_uid" not in note]
    assert len(refused) == 1, notes
    assert len(restored) == 1, notes
    assert "ABSENT" in refused[0], refused[0]
    assert "reported a set" in refused[0], refused[0]
    assert "ABSENT" not in restored[0], restored[0]


def test_one_unremovable_key_does_not_abandon_the_restorable_ones():
    m = UnremovableAfterPartialWriteManager({"a": 1, "b": 2}, unremovable="a")
    with pytest.raises(ValueError, match="write failed") as caught:
        upsert_values(m, {"a": 10, "b": "twenty"})
    assert m.store == {"a": 10, "b": 2}
    notes = getattr(caught.value, "__notes__", [])
    held = [note for note in notes if "STILL HOLD" in note]
    assert len(held) == 1, notes
    assert "a=10" in held[0], held[0]
    assert "lock timeout" in held[0], held[0]


def test_an_interrupted_rollback_never_replaces_the_error_that_refused_the_write():
    m = InterruptedRestoreManager({"q_age": 40, "q_patient_id": "P1"}, dtypes={"q_age": int})
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"q_age": "forty"})
    assert type(caught.value) is ValueError
    assert str(caught.value) == "dtype mismatch for 'q_age': expected int"
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1, notes
    assert "KeyboardInterrupt" in notes[0], notes[0]
    assert "q_age" in notes[0] and "40" in notes[0], notes[0]


def test_a_failed_restore_never_replaces_the_error_that_refused_the_write():
    m = AmbiguousRestoreManager({"parent_roi": "D1:TMA0"}, ambiguous="parent_roi")
    with pytest.raises(ValueError, match="could not be validated") as caught:
        upsert_values(m, {"parent_roi": RecordRef("other"), m.UNREGISTERED: "x"})
    notes = getattr(caught.value, "__notes__", [])
    assert any("parent_roi" in note and "ABSENT" in note for note in notes), notes
    assert any("Ambiguous match" in note for note in notes), notes
    assert "parent_roi" not in m.store


def test_one_unrestorable_key_does_not_abandon_the_restorable_ones():
    m = AmbiguousRestoreManager(
        {"parent_roi": "D1:TMA0", "roi_id": "R0C0"}, ambiguous="parent_roi",
    )
    with pytest.raises(ValueError, match="could not be validated"):
        upsert_values(
            m, {"parent_roi": RecordRef("other"), "roi_id": "R1C1", m.UNREGISTERED: "x"},
        )
    assert m.store == {"roi_id": "R0C0"}


def test_a_rollback_that_cannot_run_at_all_still_reports_the_refusal():
    m = UnreadableAfterFailureManager(
        {"q_age": 40, "q_patient_id": "P1"}, dtypes={"q_age": int},
    )
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"q_age": "forty", "q_note": "checked"})
    assert type(caught.value) is ValueError
    assert str(caught.value) == "dtype mismatch for 'q_age': expected int"
    assert m.store == {"q_patient_id": "P1"}
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1, notes
    note = notes[0]
    assert "could not roll the write back" in note, note
    assert "q_age" in note and "40" in note, note
    assert "q_note" in note and "checked" in note, note
    assert "connection unusable" in note, note


def test_a_captured_key_left_holding_the_refused_value_is_named_as_such():
    m = PartlyWrittenThenUnreadableManager({"q_age": 40, "q_patient_id": "P1"})
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"q_age": 41, "q_patient_id": "P2"})
    assert m.store == {"q_age": 41}
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1, notes
    note = notes[0]
    assert "could not roll the write back" in note, note
    assert "40" in note and "41" in note, note
    assert "'P1'" in note and "'P2'" in note, note


def test_a_restored_value_is_capped_in_the_note():
    def_text = "Bootstrap: docker\n" + "x" * 5000
    docker_ref = "ghcr.io/example-org/croi@sha256:" + "0" * 64
    m = RejectingManager(
        {"def_text": def_text, "docker_ref": docker_ref},
        dtypes={"docker_ref": str},
    )
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"def_text": "Bootstrap: docker\n", "docker_ref": 17})
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1
    note = notes[0]
    assert repr(def_text) not in note
    assert len(note) < 500
    assert "..." in note
    assert str(len(repr(def_text))) in note
    assert docker_ref in note


def test_a_rollback_with_nothing_to_restore_says_nothing():
    m = RejectingManager({"q_patient_id": "P1"}, dtypes={"q_age": int})
    with pytest.raises(ValueError) as caught:
        upsert_values(m, {"q_age": "forty"})
    assert getattr(caught.value, "__notes__", []) == []


def test_refused_environment_stamp_keeps_the_previous_provenance(_feature_registry):
    from clinicalomx.environments import F_DEF_TEXT, F_DOCKER_REF, _stamp

    art = Anchored(RejectingManager(
        {F_DOCKER_REF: "ghcr.io/example-org/croi@sha256:old", F_DEF_TEXT: "Bootstrap: docker\n"},
        dtypes={F_DOCKER_REF: str, F_DEF_TEXT: str},
    ))
    with pytest.raises(ValueError):
        _stamp(art, {F_DOCKER_REF: 17})
    assert art.features.store == {
        F_DOCKER_REF: "ghcr.io/example-org/croi@sha256:old",
        F_DEF_TEXT: "Bootstrap: docker\n",
    }


def test_refused_set_state_leaves_the_record_in_its_previous_state(_feature_registry):
    from clinicalomx.review import STATE_FEATURE, get_state, set_state

    record = Anchored(RejectingManager(
        {STATE_FEATURE: "segmentation_pending", "roi_uid": "R1"},
        dtypes={STATE_FEATURE: str},
    ))
    with pytest.raises(ValueError):
        set_state(record, 42)
    assert get_state(record) == "segmentation_pending"
    assert record.features.store == {STATE_FEATURE: "segmentation_pending", "roi_uid": "R1"}


def test_set_state_replaces_the_state_and_returns_the_record(_feature_registry):
    from clinicalomx.review import STATE_FEATURE, set_state

    record = Anchored(FakeManager({STATE_FEATURE: "segmentation_pending", "roi_uid": "R1"}))
    assert set_state(record, "segmentation_approved") is record
    assert record.features.store == {STATE_FEATURE: "segmentation_approved", "roi_uid": "R1"}
