"""The HITL gate: the only place a decision advances a unit's state.

  - ACCEPT: clinicalomx.confirm sets the unit to the gate's ``approved`` state and emits the gate
             event. An Automation then runs the persist stage.
  - REJECT: sets the unit to the gate's terminal ``rejected`` state. No event.

Runs on the VM, in the vm-process venv. Metadata and events only, no bytes.

CLI:
    python -m qptiff_pipeline.orchestration.gate accept general <wholeslide_uid>
    python -m qptiff_pipeline.orchestration.gate reject  core    <tma_uid>
"""

from __future__ import annotations

import argparse

from qptiff_pipeline.domain import states


def decide(
    stage: str,
    unit_uid: str,
    decision: states.Decision,
    payload: dict | None = None,
) -> dict:
    """Apply a gate ``decision`` to the unit ROI ``unit_uid`` for ``stage``. Returns a summary.

    ACCEPT advances to approved and emits the gate event. REJECT is terminal.
    """
    import lamindb as ln
    from clinicalomx import confirm, set_state

    from qptiff_pipeline.orchestration.events import emit

    gate = states.GATES[stage]
    record = ln.Record.get(uid=unit_uid)

    if decision is states.Decision.ACCEPT:
        confirm(record, gate.approved, event=gate.event, payload=payload or {}, emit=emit)
        return {"unit": record.name, "state": gate.approved, "emitted": gate.event}
    if decision is states.Decision.REJECT:
        set_state(record, gate.rejected)
        return {"unit": record.name, "state": gate.rejected, "emitted": None}
    raise ValueError(f"decision {decision!r} not supported yet (accept/reject only)")


def reopen(stage: str, unit_uid: str, *, cascade: bool = False) -> dict:
    """Send a decided unit back to its gate's ``pending`` state for re-review.

    For the general stage it also supersedes the slide's still-``registered`` TMAs. A later
    re-accept re-registers them.

    Refuses if any of the slide's TMAs has reached core. ``cascade=True`` resets instead: the slide
    goes to ``general_resetting`` and a ``slide.reset`` event fires. The reset-slide flow on the HPC
    then deletes the downstream core/bgnorm records and artifacts, purges harvested training pairs,
    supersedes the TMAs and sets the slide to ``general_pending``.
    """
    from pathlib import Path

    import lamindb as ln
    from clinicalomx import get_state, set_state

    from qptiff_pipeline.orchestration.events import emit

    gate = states.GATES[stage]
    record = ln.Record.get(uid=unit_uid)

    superseded: list[str] = []
    if stage == "general":
        # Find the TMAs and the qptiff registered to this slide.
        tma_type = ln.Record.filter(name=states.TMA_TYPE, is_type=True).one_or_none()
        src = record.features.get_values().get("source_image")
        qptiff = (
            src if isinstance(src, ln.Artifact) else (ln.Artifact.get(key=src) if src else None)
        )
        if tma_type is not None and qptiff is not None:
            base = Path(qptiff.key).stem
            tmas = list(ln.Record.filter(type=tma_type, name__startswith=f"{base}:TMA"))
            advanced = [
                t.name
                for t in tmas
                if get_state(t) not in (states.S.REGISTERED, states.S.SUPERSEDED, None)
            ]
            if cascade:
                # Teardown runs on the HPC, where the bytes are. reset-slide finishes at
                # general_pending.
                set_state(record, states.S.GENERAL_RESETTING)
                emit(
                    states.SLIDE_RESET_EVENT,
                    resource={"prefect.resource.id": f"clinicalomx.record.{record.uid}"},
                    payload={"record_uid": record.uid, "slide": record.name},
                )
                return {
                    "unit": record.name,
                    "state": states.S.GENERAL_RESETTING,
                    "emitted": states.SLIDE_RESET_EVENT,
                    "resetting_tmas": [t.name for t in tmas],
                }
            if advanced:
                raise RuntimeError(
                    f"cannot reopen {record.name}: {len(advanced)} TMA(s) already in core "
                    f"({', '.join(advanced)}); resolve/reopen those first, or pass cascade=True "
                    f"to reset (trash) their downstream data"
                )
            for t in tmas:
                if get_state(t) == states.S.REGISTERED:
                    set_state(t, states.S.SUPERSEDED)
                    superseded.append(t.name)

    set_state(record, gate.pending)
    return {"unit": record.name, "state": gate.pending, "superseded_tmas": superseded}


def _main() -> None:
    ap = argparse.ArgumentParser(description="Apply a gate decision (accept/reject/reopen).")
    ap.add_argument("decision", choices=["accept", "reject", "reopen"])
    ap.add_argument("stage", choices=list(states.GATES))
    ap.add_argument("unit_uid", help="uid of the unit ROI (WholeSlide for general, TMA for core)")
    ap.add_argument(
        "--cascade",
        action="store_true",
        help="reopen general: RESET downstream core/bgnorm (trash records+artifacts) instead of refusing",
    )
    args = ap.parse_args()
    if args.decision == "reopen":
        print(reopen(args.stage, args.unit_uid, cascade=args.cascade))
    else:
        print(decide(args.stage, args.unit_uid, states.Decision(args.decision)))


if __name__ == "__main__":
    _main()
