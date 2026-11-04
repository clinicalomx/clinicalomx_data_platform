"""The one function pipeline code emits a domain event through.

Automations match on the event name: renaming an event stops the stage it triggers. ``emit`` has
the signature ``clinicalomx.confirm(emit=...)`` expects.
"""
from __future__ import annotations


def emit(event: str, *, resource: dict[str, str], payload: dict) -> None:
    """Publish ``event`` to Prefect, where an Automation may run the next stage."""
    from prefect.events import emit_event

    emit_event(event=event, resource=resource, payload=payload)
