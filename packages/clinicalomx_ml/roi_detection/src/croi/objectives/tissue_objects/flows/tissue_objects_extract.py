"""The offline tissue_objects extraction as a flow: the two substrates tma_core trains on.

Not deployed. By hand: ``python -m croi tissue_objects extract-tma-crops`` and ``extract-pyramid``.
Imports other than prefect stay inside the flow body, so ``prefect deploy`` needs only prefect.
"""
from __future__ import annotations

from typing import Literal

from prefect import flow, get_run_logger

OBJECTIVE = "tissue_objects"

CROPS_CONFIG = "4_extract_tma_crops.yaml"
PYRAMID_CONFIG = "5_extract_pyramid_low_scale.yaml"


@flow(name="general-extract", log_prints=True)
def extract_flow(
    only: Literal["crops", "pyramid"] | None = None,
    limit: int | None = None,
    overrides: dict | None = None,
) -> None:
    """Cut the TMA crop release and render the pyramid projection release.

    only: run just one of the two; default runs both.
    limit: cap the slides each stage processes; ``limit=1`` is a dry run.
    overrides: dict deep-merged over each stage config that runs. A key only one stage reads
        needs ``only``.
    """
    logger = get_run_logger()

    from croi.objectives.tissue_objects import environment
    from croi.objectives.tissue_objects.config import (
        ExtractPyramidConfig,
        ExtractTmaCropsConfig,
    )

    # Validate every config before either stage writes.
    crops = (environment.load_config(CROPS_CONFIG, ExtractTmaCropsConfig, overrides)
             if only in (None, "crops") else None)
    pyramid = (environment.load_config(PYRAMID_CONFIG, ExtractPyramidConfig, overrides)
               if only in (None, "pyramid") else None)
    from croi.objectives.tissue_objects import extract

    if crops is not None:
        logger.info(f"{OBJECTIVE} extract: tma_crops")
        extract.run_tma_crops(crops, limit=limit)
    if pyramid is not None:
        logger.info(f"{OBJECTIVE} extract: pyramid_low_scale")
        extract.run_pyramid_low_scale(pyramid, limit=limit)
    logger.info(f"{OBJECTIVE} extract done")
