"""The foundation vocabulary as importable constants. No imports: depend on this freely.

Only names the framework reads or writes, plus shared conventions no single repo owns. Consumers
declare their own names with :class:`clinicalomx.vocab.Vocabulary`; see ``docs/extending.md``.
Every constant here must appear in ``vocab/data/features.csv`` or ``types.yaml``
(``tests/test_names.py``).
"""

from __future__ import annotations


class TYPES:
    """``ln.Record`` type records (``is_type=True``)."""

    # Clinical spine.
    SOURCE = "Source"
    COHORT = "Cohort"
    PATIENT = "Patient"

    # Physical and spatial. The whole-slide type is WHOLE_SLIDE; there is no "Slide".
    ROI = "ROI"
    TMA = "TMA"
    CORE = "Core"
    WHOLE_SLIDE = "WholeSlide"
    TILE = "Tile"

    # Framework internals, created at apply time.
    SEED_VERSION = "SeedVersion"


class FEATURES:
    """``ln.Feature`` names in the foundation."""

    # ROI relations: written by clinicalomx.roi.
    ROI_UID = "roi_uid"
    ROI_NAME = "roi_name"
    ROI_TYPE = "roi_type"
    PARENT_ROI = "parent_roi"

    # Gate state: read and written by clinicalomx.review.
    PIPELINE_STATE = "pipeline_state"

    # Environment pointers: written by clinicalomx.environments.
    REGISTERED_ENV = "registered_env"
    DOCKER_REF = "docker_ref"
    DEF_TEXT = "def_text"

    # Clinical spine entity links.
    SOURCE = "source_feature"
    COHORT = "cohort_feature"
    PATIENT = "patient"
    PATIENT_ID = "patient_id"
    #: The Slide link, typed to WholeSlide. `slide_feature` is deprecated.
    SLIDE = "slide"

    # Generic provenance.
    SOURCE_ARTIFACT = "source_artifact"
    SOURCE_IMAGE = "source_image"

    # ROI geometry interchange, read by the detection, dearray and segmentation repos.
    ROI_ID = "roi_id"
    ROI_PARQUET = "roi_parquet"
    SCENE = "scene"
    SCALE = "scale"
    COORDINATE_SPACE = "coordinate_space"


PROJECT = "Clinicalomxdb Schema Creation"


def _values(cls) -> list[str]:
    return [v for k, v in vars(cls).items() if not k.startswith("_") and isinstance(v, str)]


ALL_TYPES = _values(TYPES)
ALL_FEATURES = _values(FEATURES)
