"""Stage selector: the review stages as one numbered row of buttons, in pipeline order.

    1 General detections   2 Core detections   3 Queue for bgnorm   4 bgnorm results

Each label carries its queue count. The chosen stage is written to ``st.session_state["stage"]``
and returned. Counts are passed in, so this module needs no lamindb or Prefect.
"""
from __future__ import annotations

import streamlit as st

STEPS: list[dict[str, str]] = [
    {
        "stage": "general", "label": "General detections",
        "does": "WholeSlide: cuts the slide thumbnail into TMA / tissue_section ROIs",
    },
    {
        "stage": "core", "label": "Core detections",
        "does": "TMA: cuts + grids one TMA into row,col cores (dearray)",
    },
    {
        "stage": "triage", "label": "Queue for bgnorm",
        "does": "TMA: routes TMAs (bgnorm vs H&E); fans out one bgnorm job per core",
    },
    {
        "stage": "bgnorm", "label": "bgnorm results",
        "does": "Core: collapses the per-core bgnorm results into one TMA decision",
    },
]
STEP_BY_STAGE = {s["stage"]: s for s in STEPS}

# What the count on a step means.
_QUEUE_WORD = {"general": "pending", "core": "pending", "triage": "waiting", "bgnorm": "pending"}


def _label(n: int, step: dict, counts: dict[str, int] | None) -> str:
    label = f"{n}. {step['label']}"
    if counts and step["stage"] in counts:
        label += f" · {counts[step['stage']]} {_QUEUE_WORD[step['stage']]}"
    return label


def render(counts: dict[str, int] | None = None) -> str:
    """Draw the stage row and return the selected stage. ``counts`` is ``{stage: n}``."""
    st.session_state.setdefault("stage", "general")
    active = st.session_state["stage"]
    if active not in STEP_BY_STAGE:
        active = st.session_state["stage"] = "general"
    for n, (col, step) in enumerate(zip(st.columns(len(STEPS)), STEPS), start=1):
        if col.button(
            _label(n, step, counts),
            key=f"nav-{step['stage']}",
            type="primary" if step["stage"] == active else "secondary",
            width="stretch",
            help=step["does"],
        ):
            st.session_state["stage"] = step["stage"]
            st.rerun()
    return st.session_state["stage"]
