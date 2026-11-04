"""The pipeline's state model: the states a unit can be in, and how it moves between them.

:class:`S` holds the states, :class:`Decision` the answers at a gate, :class:`Gate` a human STOP.
:data:`STAGES` is the topology. ``GATES``, ``STAGE_ENTRY`` and ``ENTRY_EVENTS`` derive from it.
Read and write a unit's state with :func:`clinicalomx.get_state` and :func:`clinicalomx.set_state`.

Nothing here names Prefect. :mod:`qptiff_pipeline.orchestration.automations` renders the table to
Prefect Automations. Feature names live in :mod:`qptiff_pipeline.domain.vocab`.

Every state a gate or a stage names is checked against :class:`S` at import.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TypedDict

from clinicalomx.names import FEATURES, TYPES

# ROI type names come from the framework. Never respell them here.
SLIDE_TYPE = TYPES.WHOLE_SLIDE
TMA_TYPE = TYPES.TMA
CORE_TYPE = TYPES.CORE

# Framework-owned provenance features.
F_SLIDE = FEATURES.SLIDE
F_SOURCE_IMAGE = FEATURES.SOURCE_IMAGE
F_SOURCE_ARTIFACT = FEATURES.SOURCE_ARTIFACT

# The lamindb Project every run of this repo is scoped to. Applied by tracking.track_prefect_run.
PROJECT_NAME = "Clinicalomx QPTIFF pipeline."
PROJECT_DESCRIPTION = (
    "Human-gated serving pipeline that turns raw whole-slide .qptiff scans into background-"
    "normalised per-core FOVs, orchestrating three libraries (clinical_roi = DL ROI/core "
    "detection, bgnorm = background normalisation, clinicalomx = the lamindb foundation). Stages: "
    "(1) register-qptiffs: ingest .qptiff as in-place Artifacts; (2) check-qptiffs: integrity "
    "gate, quarantine unreadable scans; (3) mint-slides: create WholeSlide Records; "
    "(4) image-type validation: classify multiplex vs H&E (per-slide, confirmable in triage); "
    "(5) general-detect + review: detect TMA / tissue-section ROIs, human-review the boxes; "
    "(6) general-persist: map accepted ROIs to full-res, register TMA Records; (7) core-detect "
    "+ review: dearray each TMA into cores, human-review the grid; (8) core-persist: register "
    "Core Records; (9) triage: route each TMA to the bgnorm queue or the H&E lane; (10) bgnorm"
    ": per-core background normalisation (one MLflow experiment per core), written as zarr; "
    "(11) harvest-feedback: collect corrected (image, label) pairs for retraining. Derivatives "
    "live under the 'clinicalomx-qptiff-pipeline/<slide>/...' key layout (see keys.py)."
)


class Decision(enum.StrEnum):
    """A gate decision, the same shape from a human UI or a LangGraph agent."""

    ACCEPT = "accept"
    REJECT = "reject"
    RERUN = "rerun"  # reserved for the future bgnorm decision gate


class S(enum.StrEnum):
    """``pipeline_state`` values, prefixed by stage, attached to the unit ROI that stage works on.

    ``registered`` means created and waiting for its next stage. ``str(S.X)`` is the bare value.
    """

    REGISTERED = "registered"

    GENERAL_PENDING = "general_pending"  # STOP 1: review the slide's ROIs
    GENERAL_APPROVED = "general_approved"
    GENERAL_REJECTED = "general_rejected"  # terminal
    GENERAL_FAILED = "general_failed"  # terminal: detect could not read/process the slide
    GENERAL_RESETTING = "general_resetting"  # cascade teardown of downstream core/bgnorm in flight

    CORE_PENDING = "core_pending"  # STOP 2: review this TMA's cores
    CORE_APPROVED = "core_approved"
    CORE_REJECTED = "core_rejected"  # terminal
    CORE_FAILED = "core_failed"  # non-terminal: detect could not read/grid the TMA (retryable)

    # Triage (human) routes a core_approved TMA to one of these before any bgnorm compute runs.
    BGNORM_QUEUED = "bgnorm_queued"  # in the bgnorm queue pool, awaiting the runner
    BGNORM_RUNNING = "bgnorm_running"  # runner dispatched per-core jobs; awaiting completion
    HE_PENDING = "he_pending"  # routed to the H&E lane (no bgnorm; future downstream consumer)

    BGNORM_PENDING = "bgnorm_pending"
    COMPLETE = "complete"
    BGNORM_FAILED = "bgnorm_failed"  # terminal

    SUPERSEDED = "superseded"  # a reopened-and-replaced component (audit)


@dataclass(frozen=True)
class Gate:
    """A human/agent STOP on a unit ROI. The stage parks ``unit_type`` at ``pending``;
    a decision advances it to ``approved`` (emitting ``event``) or ``rejected`` (terminal)."""

    unit_type: str
    pending: str
    approved: str
    rejected: str
    event: str


@dataclass(frozen=True)
class StageSpec:
    """One pipeline stage.

    ``entry_state`` is where a unit of ``unit_type`` becomes eligible. ``entry_event`` fires when it
    gets there. ``gate`` is the STOP the stage parks the unit at, or ``None`` for no human review.
    """

    name: str
    unit_type: str
    entry_state: S
    entry_event: str
    flow: str
    gate: Gate | None = None


#: THE PIPELINE, declaratively. Adding a stage is adding a row.
STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        name="general",
        unit_type=SLIDE_TYPE,
        entry_state=S.REGISTERED,
        entry_event="clinical-roi.slide.registered",
        flow="general-detect",
        gate=Gate(SLIDE_TYPE, S.GENERAL_PENDING, S.GENERAL_APPROVED, S.GENERAL_REJECTED,
                  "clinical-roi.general.accepted"),
    ),
    StageSpec(
        name="core",
        unit_type=TMA_TYPE,
        entry_state=S.REGISTERED,
        # Per TMA, emitted by general-persist (payload `tma_uid`, not `record_uid`).
        entry_event="clinical-roi.tma.registered",
        flow="core-detect",
        gate=Gate(TMA_TYPE, S.CORE_PENDING, S.CORE_APPROVED, S.CORE_REJECTED,
                  "clinical-roi.core.accepted"),
    ),
    StageSpec(
        name="bgnorm",
        unit_type=TMA_TYPE,
        # Only TMAs a reviewer queued at triage. Triage itself works on core_approved.
        entry_state=S.BGNORM_QUEUED,
        # Emitted per core (payload `core_uid`) by the bgnorm-runner: one flow run per core.
        entry_event="clinical-roi.core.bgnorm_requested",
        flow="bgnorm-core",
        # A decision gate: the stage runs first and parks the TMA at bgnorm_pending. The human
        # accepts (complete) or fails it (bgnorm_failed). Both are terminal.
        gate=Gate(TMA_TYPE, S.BGNORM_PENDING, S.COMPLETE, S.BGNORM_FAILED,
                  "clinical-roi.bgnorm.accepted"),
    ),
)

STAGE: dict[str, StageSpec] = {s.name: s for s in STAGES}

# Derived views.
#: ``{stage: Gate}`` for every stage that has one.
GATES: dict[str, Gate] = {s.name: s.gate for s in STAGES if s.gate is not None}
#: ``{stage: (unit type, state at which a unit becomes eligible)}``.
STAGE_ENTRY: dict[str, tuple[str, str]] = {s.name: (s.unit_type, s.entry_state) for s in STAGES}
#: ``{stage: event emitted when a unit enters its start state}``.
ENTRY_EVENTS: dict[str, str] = {s.name: s.entry_event for s in STAGES}

# Emitted by a cascade reopen of a WholeSlide whose TMAs already reached core. reset-slide tears
# down the downstream work, then moves the slide from general_resetting to general_pending.
SLIDE_RESET_EVENT = "clinical-roi.slide.reset"

#: Terminal states: nothing advances a unit out of one.
TERMINAL_STATES: frozenset[S] = frozenset({
    S.GENERAL_REJECTED, S.GENERAL_FAILED, S.CORE_REJECTED,
    S.COMPLETE, S.BGNORM_FAILED, S.SUPERSEDED,
})


class PipelineState(TypedDict, total=False):
    """One unit's position in the graph, as a plain mapping an agentic driver reads and writes.

    ``total=False``: a unit at ingest has only a uid and a state.
    """

    record_uid: str
    record_name: str
    unit_type: str
    state: S
    stage: str
    decision: str
    #: Free-form, stage-specific: the reviewer's note, a failure reason, bgnorm channel names.
    detail: dict


def stage_for(unit_type: str, state: str) -> StageSpec | None:
    """The stage a unit of ``unit_type`` in ``state`` is eligible for, or ``None``.

    Two stages share ``S.REGISTERED`` as their entry state. The unit type tells them apart.
    """
    for spec in STAGES:
        if spec.unit_type == unit_type and spec.entry_state == state:
            return spec
    return None


def gate_for_state(state: str) -> tuple[StageSpec, Gate] | None:
    """The stage and gate a unit is parked at, or ``None`` if it is not awaiting a decision."""
    for spec in STAGES:
        if spec.gate is not None and spec.gate.pending == state:
            return spec, spec.gate
    return None


def _validate() -> None:
    """Raise if the topology names a state that is not an :class:`S` member. Runs at import."""
    known = set(S)
    for spec in STAGES:
        if spec.entry_state not in known:
            raise ValueError(f"stage {spec.name!r}: entry_state {spec.entry_state!r} is not an S")
        if spec.gate is None:
            continue
        for field in ("pending", "approved", "rejected"):
            value = getattr(spec.gate, field)
            if value not in known:
                raise ValueError(f"gate {spec.name!r}: {field} {value!r} is not an S")
    if len(GATES) != len({g.event for g in GATES.values()}):
        raise ValueError("two gates share one event name; each must be distinguishable")
    if len(STAGE) != len(STAGES):
        raise ValueError("two stages share one name")
    unreachable = TERMINAL_STATES & {s.entry_state for s in STAGES}
    if unreachable:
        raise ValueError(f"terminal states used as a stage entry: {sorted(unreachable)}")


_validate()
