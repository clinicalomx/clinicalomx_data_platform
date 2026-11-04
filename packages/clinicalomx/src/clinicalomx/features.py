"""Feature-value helpers shared by the ROI layer and the vocabulary seeder."""

from __future__ import annotations

from typing import Any

_NOTE_VALUE_CHARS = 200


def _note_value(value: Any) -> str:
    text = repr(value)
    if len(text) <= _NOTE_VALUE_CHARS:
        return text
    return f"{text[:_NOTE_VALUE_CHARS]}... (truncated, {len(text)} chars)"


def _named(values: dict[str, Any]) -> str:
    return ", ".join(f"{key}={_note_value(value)}" for key, value in values.items())


def _restore(manager: Any, error: BaseException, values: dict[str, Any],
             present: dict[str, Any]) -> None:
    """Put back what the failed write removed, per key, and note the outcome on ``error``."""
    current = manager.get_values()
    wrote = [key for key in values
             if key in current and (key not in present or current[key] != present[key])]
    unremoved: dict[str, tuple[Any, Exception]] = {}
    for key in wrote:
        try:
            manager.remove_values([key])
        except Exception as removal_error:
            unremoved[key] = (current[key], removal_error)
    needed = {key: value for key, value in present.items()
              if key not in unremoved and (key not in current or current[key] != value)}
    restored: dict[str, Any] = {}
    refused: dict[str, Any] = {}
    failed: dict[str, tuple[Any, Exception]] = {}
    for key, value in needed.items():
        if isinstance(value, set):
            refused[key] = value
            continue
        try:
            manager.add_values({key: value})
        except Exception as restore_error:
            failed[key] = (value, restore_error)
        else:
            restored[key] = value
    if restored:
        error.add_note(f"upsert_values rolled the write back and restored {_named(restored)}")
    if refused:
        error.add_note(
            "upsert_values did not restore these keys and they are now ABSENT, because "
            "get_values reported a set, which cannot be replayed as it was written: "
            f"{_named(refused)}"
        )
    if failed:
        error.add_note(
            "upsert_values could not restore these keys and they are now ABSENT: "
            + ", ".join(f"{key}={_note_value(value)} ({restore_error!r})"
                        for key, (value, restore_error) in failed.items())
        )
    if unremoved:
        error.add_note(
            "upsert_values could not take back what the refused write left on these keys, so "
            "they STILL HOLD it rather than what was captured: "
            + ", ".join(f"{key}={_note_value(value)} ({removal_error!r})"
                        for key, (value, removal_error) in unremoved.items())
        )


def upsert_values(manager: Any, values: dict[str, Any]) -> None:
    """Set single-valued feature values, leaving every other feature untouched. Re-runs converge.

    Removes only the keys about to be written, then adds. ``add_values`` alone violates the unique
    constraint on an existing value, and ``set_values`` clears every feature first.

    A failed or interrupted write is rolled back and the original exception is re-raised; rollback
    failures are attached to it as notes, never raised in its place. A scalar restores exactly. A
    set-valued capture is not replayed and is left absent. A relational value restores by name and
    raises if the name is ambiguous. Each restored, refused or failed key is named in the notes,
    with values truncated at 200 characters.
    """
    if not values:
        return
    previous = manager.get_values()
    present = {key: previous[key] for key in values if key in previous}
    try:
        if present:
            manager.remove_values(list(present))
        manager.add_values(values)
    except BaseException as error:
        try:
            _restore(manager, error, values, present)
        except BaseException as rollback_error:
            attempted = {key: value for key, value in values.items() if key not in present}
            parts = ["upsert_values could not roll the write back and could not establish the "
                     "state it left, so read the record before deciding anything. If this call "
                     "ran inside a transaction that is now rolling back, the removal goes back "
                     "with it and the record is already as it was; if it did not, the keys "
                     "below are the ones to check."]
            if present:
                parts.append(
                    "These keys MAY hold the value captured before the write, the value the "
                    "refused write set, or nothing at all: "
                    + ", ".join(
                        f"{key}={_note_value(value)} (refused write: "
                        f"{_note_value(values[key])})"
                        for key, value in present.items()
                    )
                    + "."
                )
            if attempted:
                parts.append(
                    "These keys MAY hold a value from the refused write, or nothing at all: "
                    f"{_named(attempted)}."
                )
            parts.append(f"({rollback_error!r})")
            error.add_note(" ".join(parts))
        raise
