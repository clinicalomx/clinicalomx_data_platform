"""The offline tma_core dearray as a flow, building the training substrate and its shape Artifacts.

Not deployed. By hand: ``python -m croi tma_core extract-tma-cores``. Production dearray is
``clinicalomx_pipelines``' ``flows/core_detect.py``. Imports other than prefect stay in the flow body.
"""
from __future__ import annotations

from prefect import flow, get_run_logger

OBJECTIVE = "tma_core"

CORES_CONFIG = "4_extract_tma_cores.yaml"


@flow(name="core-extract", log_prints=True)
def extract_flow(
    image: str | None = None,
    limit: int | None = None,
    overrides: dict | None = None,
) -> None:
    """Dearray every resolved QPTIFF into TMA and Core shape Artifacts and their ROI graph.

    image: dearray only this qptiff Artifact key.
    limit: cap the slides processed; ``limit=1`` is a dry run.
    overrides: dict deep-merged over the stage config.
    """
    logger = get_run_logger()

    from croi.objectives.tma_core import environment
    from croi.objectives.tma_core.config import ExtractTmaCoresConfig

    config = environment.load_config(CORES_CONFIG, ExtractTmaCoresConfig, overrides)
    from croi.objectives.tma_core import extract

    logger.info(f"{OBJECTIVE} extract: tma_cores")
    extract.run_tma_cores(config, image=image, limit=limit)
    logger.info(f"{OBJECTIVE} extract done")
