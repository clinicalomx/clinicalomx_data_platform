"""ensure-qptiffs-checked: a scheduled VM flow that re-launches check-qptiffs until none are
unchecked.

Each tick counts qptiff Artifacts with no ``qptiff_integrity`` mark. If any remain and no
check-qptiffs run is in flight, it starts one. check-qptiffs resumes where the last run stopped.
"""
from __future__ import annotations

# Put the checkout's src/ on sys.path: the worker may run without an install of this package.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

# check-qptiffs states that mean a sweep is already in flight.
_ACTIVE_STATES = ["SCHEDULED", "PENDING", "RUNNING", "PAUSED", "CANCELLING"]


@flow(name="ensure-qptiffs-checked", log_prints=True)
def ensure_qptiffs_checked(suffix: str = ".qptiff") -> dict:
    """Start check-qptiffs while any qptiff still lacks an integrity mark.

    suffix: match Artifacts whose key ends with this. Not the ``Artifact.suffix`` field.
    """
    logger = get_run_logger()


    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.lamin.queries import source_artifacts

    # Count unchecked qptiffs with one bulk read, falling back to a per-artifact scan.
    total = 0
    unchecked = 0
    feature = qptiff_vocab.INTEGRITY_FEATURE
    try:
        df = source_artifacts(suffix).to_dataframe(
            features=[feature], limit=1_000_000
        )
        total = len(df)
        unchecked = int(df[feature].isna().sum()) if feature in df.columns else total
    except Exception as exc:
        logger.warning(f"bulk count failed ({exc!r}); scanning per-artifact")
        total = unchecked = 0
        for art in source_artifacts(suffix):
            total += 1
            if art.features.get_values().get(feature) is None:
                unchecked += 1

    logger.info(f"qptiffs: {total} total, {unchecked} unchecked")
    if unchecked == 0:
        logger.info("all qptiffs checked; nothing to do")
        return {"total": total, "unchecked": 0, "triggered": False}

    # Do not start a second sweep while one is in flight.
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
            flow_filter=FlowFilter(name=FlowFilterName(any_=["check-qptiffs"])),
            flow_run_filter=FlowRunFilter(
                state=FlowRunFilterState(type=FlowRunFilterStateType(any_=_ACTIVE_STATES))
            ),
            limit=1,
        )
        if active:
            logger.info(f"check-qptiffs already in flight ({active[0].name}); not re-triggering")
            return {"total": total, "unchecked": unchecked, "triggered": False}
        dep = client.read_deployment_by_name("check-qptiffs/pipeline-check-qptiffs")
        run = client.create_flow_run_from_deployment(dep.id)

    logger.info(f"triggered check-qptiffs ({run.name}) for {unchecked} remaining qptiff(s)")
    return {"total": total, "unchecked": unchecked, "triggered": True, "run": run.name}
