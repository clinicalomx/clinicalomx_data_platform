"""data-tests: run the declared data invariants over the instance. READ-ONLY.

Runs the framework's ROI checks and this repo's pipeline checks. Fails the run when an
``error``-severity check fails. ``warn`` findings are reported only. Runs on the HPC, so the
storage-backed checks are included. ``include_storage=False`` runs on metadata alone.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


@flow(name="data-tests", log_prints=True, retries=1, retry_delay_seconds=60)
def data_tests(
    include_storage: bool = True,
    fail_on_error: bool = True,
    only: str | None = None,
) -> dict:
    """Run the declared checks and report failures.

    include_storage: also run checks that read artifact bytes (HPC only).
    fail_on_error: raise when an ``error``-severity check fails.
    only: substring filter on check names, e.g. ``roi.`` for the ROI invariants only.
    """
    logger = get_run_logger()

    import lamindb as ln
    from clinicalomx.checks import Snapshot, run_checks, summarise
    from clinicalomx.checks.roi import framework_checks

    from qptiff_pipeline.checks.pipeline import pipeline_checks
    from qptiff_pipeline.domain import governance
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("data-tests")
    try:
        # Load the governance table: the SIF has no entry point to discover it from.
        governance.register()
        # An explicit list, not all_checks(): entry-point discovery finds nothing in the SIF and
        # would drop this repo's checks.
        declared = [*framework_checks(), *pipeline_checks()]
        checks = [c for c in declared if only is None or only in c.name]
        logger.info(f"running {len(checks)} check(s), include_storage={include_storage}")

        results = run_checks(
            checks, snapshot=Snapshot(), include_storage=include_storage, logger=logger)
        report, has_errors = summarise(results)
        print(report, flush=True)

        payload = {
            "checks": len(results),
            "errors": sum(1 for r in results if not r.passed and r.check.severity == "error"),
            "warnings": sum(1 for r in results if not r.passed and r.check.severity == "warn"),
            "failures": {r.check.name: r.failures[:50] for r in results if not r.passed},
        }
        logger.info(f"data-tests done: {payload['errors']} error(s), {payload['warnings']} warning(s)")
        if has_errors and fail_on_error:
            raise AssertionError(
                f"{payload['errors']} error-severity data check(s) failed; see the report above"
            )
        return payload
    finally:
        ln.finish()
