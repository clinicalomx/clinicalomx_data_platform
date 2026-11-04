"""Lamindb features this objective owns, declared with descriptions so `clinicalomx.vocab audit`
can attribute them.

`feature()` returns the name: import these constants rather than writing the string. A Feature
cannot be deleted once used. The ROI scalars and MLflow coordinates are also declared in
`croi.objectives.tissue_objects.vocab` from `croi.feature_names`; keep their dtypes identical.
"""
from __future__ import annotations

import lamindb as ln
from clinicalomx.vocab import Vocabulary

from croi import feature_names

#: Distinct per objective: ``discover()`` keys by owner.
VOCAB = Vocabulary("croi-core")

# --- ROI roles (multi-valued: the artifacts hanging off one ROI) --------------------
DETECTIONS = VOCAB.feature(
    "detections", ln.Artifact,
    "Artifact describing detections for this ROI: a thumbnail, a YOLO label .txt, or a "
    "low-res projection the boxes were drawn on.",
)

# --- Detection geometry, shared by both objectives (keep identical across the two) ---
DETECTION_CLASS_NAME = VOCAB.feature(
    "detection_class_name", str,
    "YOLO class the detector assigned: TMA / tissue_section / full_core / partial_core.",
)
DETECTION_CONFIDENCE = VOCAB.feature(
    "detection_confidence", float,
    "Detector confidence for this ROI's box, in [0, 1].",
)
BBOX_X1 = VOCAB.feature("bbox_x1", float, "Box left edge, smallest-pyramid-level pixels.")
BBOX_Y1 = VOCAB.feature("bbox_y1", float, "Box top edge, smallest-pyramid-level pixels.")
BBOX_X2 = VOCAB.feature("bbox_x2", float, "Box right edge, smallest-pyramid-level pixels.")
BBOX_Y2 = VOCAB.feature("bbox_y2", float, "Box bottom edge, smallest-pyramid-level pixels.")
PARQUET_ROW = VOCAB.feature(
    "parquet_row", int,
    "Positional row of this ROI within its source_artifact GeoParquet, so a crop can be "
    "derived on demand via .iloc.",
)
IS_DETECTED = VOCAB.feature(
    "is_detected", bool,
    "False for a grid cell inferred by gridding rather than detected, which therefore has "
    "no real geometry behind it.",
)
ROW = VOCAB.feature("row", int, "Zero-based grid row of a Core within its TMA.")
COL = VOCAB.feature("col", int, "Zero-based grid column of a Core within its TMA.")
CORE_COMPLETENESS = VOCAB.feature(
    "core_completeness", str,
    'Whether a Core is "full" or "partial". A feature, not a type: the shared instance has '
    "one Core type and variants are features on it.",
)

# --- MLflow coordinates stamped on a registered model / eval run --------------------
MLFLOW_RUN_ID = VOCAB.feature(
    feature_names.MLFLOW_RUN_ID, str,
    "MLflow run that produced these weights; the join key between the lamindb Artifact and "
    "the tracking server.",
)
MLFLOW_RUN_NAME = VOCAB.feature(feature_names.MLFLOW_RUN_NAME, str, "Human-readable MLflow run name.")
MLFLOW_EXPERIMENT_NAME = VOCAB.feature(
    feature_names.MLFLOW_EXPERIMENT_NAME, str,
    "MLflow experiment the run belongs to, e.g. clinical-roi/general-train.",
)
MLFLOW_ARTIFACT_URI = VOCAB.feature(
    feature_names.MLFLOW_ARTIFACT_URI, str,
    "Run artifact root on the tracking server, so the portable checkpoint is fetchable "
    "off-cluster.",
)
MODEL_URI = VOCAB.feature(
    feature_names.MODEL_URI, str,
    "Registered-model URI for these weights, models:/roi-<objective>/<version>.",
)
MODEL_FRAMEWORK = VOCAB.feature(
    feature_names.MODEL_FRAMEWORK, str,
    "Framework that reads these weights: a key of <objective>.models.detect.BACKENDS "
    "(ultralytics | rfdetr). Absent on models registered before it existed, all ultralytics.",
)

# --- core-only: the GeoParquet shape containers a dearray produces ------------------
TMA_SHAPES = VOCAB.feature(
    "tma_shapes", ln.Artifact,
    "GeoParquet holding every detected TMA grid box for one slide, one row per TMA.",
)
CORE_SHAPES = VOCAB.feature(
    "core_shapes", ln.Artifact,
    "GeoParquet holding every core box for one slide, detected and grid-inferred alike, one "
    "row per Core.",
)

#: ROI-side scalars for :func:`clinicalomx.ensure_features`. ``source_image`` and
#: ``source_artifact`` are foundation features in :class:`clinicalomx.names.FEATURES`.
ROI_SCALARS: dict[str, type] = {
    DETECTION_CLASS_NAME: str,
    DETECTION_CONFIDENCE: float,
    CORE_COMPLETENESS: str,
    ROW: int,
    COL: int,
    BBOX_X1: float,
    BBOX_Y1: float,
    BBOX_X2: float,
    BBOX_Y2: float,
    PARQUET_ROW: int,
    IS_DETECTED: bool,
}
