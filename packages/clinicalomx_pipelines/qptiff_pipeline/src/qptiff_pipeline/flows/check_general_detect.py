"""check-general-detect: a cheap VM gate in front of the HPC general-detect job.

Starts general-detect in batch mode only when at least one WholeSlide is ``registered`` and no
general-detect run is in flight. Triggered after each mint-slides and each general-detect run.
The deployment's concurrency limit of 1 keeps the in-flight check reliable.
"""
from __future__ import annotations

# Put the checkout's src/ on sys.path: the worker may run without an install of this package.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

# general-detect states that mean a run is already in flight.
_ACTIVE_STATES = ["SCHEDULED", "PENDING", "RUNNING", "PAUSED", "CANCELLING"]


@flow(name="check-general-detect", log_prints=True)
def check_general_detect(slide_uids: list[str] | None = None) -> dict:
    """Start general-detect in batch mode if registered slides exist and none is in flight.

    slide_uids: informational only. The selection is re-derived here and again by general-detect.
    """
    logger = get_run_logger()

    from qptiff_pipeline.lamin.queries import select_registered_slide_uids

    registered = select_registered_slide_uids()
    if not registered:
        logger.info("no registered WholeSlides; skipping general-detect submit")
        return {"registered": 0, "triggered": False}

    # An in-flight run will sweep the whole registered backlog when it starts.
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import (
        FlowFilter,
        FlowFilterName,
        FlowRunFilter,
        FlowRunFilterState,
        FlowRunFilterStateType,
    )

    with get_client(sync_client=True) as client:
        active = client.read_flow_runs(
            flow_filter=FlowFilter(name=FlowFilterName(any_=["general-detect"])),
            flow_run_filter=FlowRunFilter(
                state=FlowRunFilterState(type=FlowRunFilterStateType(any_=_ACTIVE_STATES))
            ),
            limit=1,
        )
        if active:
            logger.info(
                f"general-detect already in flight ({active[0].name}); not re-triggering "
                f"({len(registered)} registered slide(s) will be swept by it)"
            )
            return {"registered": len(registered), "triggered": False}
        dep = client.read_deployment_by_name("general-detect/pipeline-general-detect")
        # No parameters: general-detect runs in batch mode.
        run = client.create_flow_run_from_deployment(dep.id)

    logger.info(f"triggered general-detect ({run.name}) for {len(registered)} registered slide(s)")
    return {"registered": len(registered), "triggered": True, "run": run.name}
