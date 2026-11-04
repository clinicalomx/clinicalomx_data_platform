"""Review-gate state machine: human-gated pipeline stops stored as Record state.

A stage parks a lamindb Record (slide, TMA, core) in a ``*_pending`` state. A human reviews and
calls :func:`confirm`, which moves it to ``*_approved`` and emits an event the next stage's
orchestrator (such as a Prefect Automation) matches. The state is a single string Feature (default
``pipeline_state``), queryable in SQL through :func:`list_by_state`. Prefect is never imported at
module load; the emitter is injected with :func:`register_event_emitter`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from clinicalomx.features import upsert_values
from clinicalomx.names import FEATURES

#: The state feature name, declared in names.py.
STATE_FEATURE = FEATURES.PIPELINE_STATE

# Set by the consuming project.
# Signature: emit(event: str, *, resource: dict[str, str], payload: dict).
_EVENT_EMITTER: Callable[..., Any] | None = None


def register_event_emitter(fn: Callable[..., Any]) -> None:
    """Register the callable :func:`confirm` uses to emit a gate event.

    Signature: ``emit(event: str, *, resource: dict[str, str], payload: dict)``.
    Use :func:`prefect_emitter`, or any callable with that signature.
    """
    global _EVENT_EMITTER
    _EVENT_EMITTER = fn


def prefect_emitter(event: str, *, resource: dict[str, str], payload: dict) -> None:
    """An emitter that publishes a Prefect event. Needs ``clinicalomx[prefect]``. Register once::

        from clinicalomx import register_event_emitter, prefect_emitter
        register_event_emitter(prefect_emitter)
    """
    try:
        from prefect.events import emit_event
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "prefect_emitter needs Prefect: install clinicalomx[prefect] (or pip install prefect)"
        ) from e
    emit_event(event=event, resource=resource, payload=payload)


# State accessors.
def ensure_state_feature(*, feature: str = STATE_FEATURE) -> None:
    """Get-or-create the string Feature that holds the state."""
    import lamindb as ln
    if ln.Feature.filter(name=feature).one_or_none() is None:
        ln.Feature(name=feature, dtype=str).save()


def get_state(record, *, feature: str = STATE_FEATURE) -> str | None:
    """The Record's current pipeline state, or ``None`` if unset."""
    val = record.features.get_values().get(feature)
    return str(val) if val is not None else None


def set_state(record, state: str, *, feature: str = STATE_FEATURE):
    """Replace the Record's pipeline state. Returns the Record.

    A refused write restores the old state.
    """
    ensure_state_feature(feature=feature)
    upsert_values(record.features, {feature: state})
    return record


def list_by_state(
    records: Iterable,
    state: str,
    *,
    feature: str = STATE_FEATURE,
) -> list:
    """Records from ``records`` currently in ``state``. Filters in SQL for a queryset."""
    filterer = getattr(records, "filter", None)
    if filterer is not None:
        return list(filterer(
            values_json__feature__name=feature,
            values_json__value=state,
        ))
    return [r for r in records if get_state(r, feature=feature) == state]


def confirm(
    record,
    approved_state: str,
    *,
    event: str | None = None,
    payload: dict | None = None,
    emit: Callable[..., Any] | None = None,
    feature: str = STATE_FEATURE,
):
    """Clear a stop: move ``record`` to ``approved_state``, then emit ``event``. Returns the Record.

    ``event`` defaults to ``approved_state`` and carries the Record's uid. The emitter is the
    registered one unless ``emit=`` is given. With no emitter, the state still advances.
    """
    set_state(record, approved_state, feature=feature)
    evt = event or approved_state
    emitter = emit or _EVENT_EMITTER
    if emitter is not None:
        emitter(
            evt,
            resource={"prefect.resource.id": f"clinicalomx.record.{record.uid}"},
            payload={"record_uid": record.uid, "state": approved_state, **(payload or {})},
        )
    return record
