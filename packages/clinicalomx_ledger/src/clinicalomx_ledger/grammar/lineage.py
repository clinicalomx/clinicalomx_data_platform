"""Read view of the run that produced a claim. Imports no lamindb.

A claim may have no run, such as a human assertion: `of(None)` is None and every field is
optional.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lineage:
    """What the run that produced a claim says about how it was produced. Never stored."""

    run_uid: str = ""
    started_at: str = ""


def of(run) -> Lineage | None:
    """The lineage of ``run``, or None when there is no run."""
    if run is None:
        return None
    return Lineage(
        run_uid=str(getattr(run, "uid", "") or ""),
        started_at=str(getattr(run, "started_at", "") or ""),
    )
