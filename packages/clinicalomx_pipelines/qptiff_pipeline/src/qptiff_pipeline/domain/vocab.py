"""The vocabulary this repo owns: every feature name only qptiff_pipeline reads or writes.

``python -m clinicalomx.vocab audit`` reports these under :data:`OWNER`. Declaring one name with two
dtypes raises.

This module must import without a database, because everything imports it through ``states``.
Artifact dtypes are the :data:`ARTIFACT` sentinel. lamindb is imported only inside
:func:`vocabulary`.
"""
from __future__ import annotations

from typing import Any

from qptiff_pipeline.domain import qptiff as qptiff_vocab
from qptiff_pipeline.domain.declare import ARTIFACT, Declarer

OWNER = "clinicalomx-pipeline"  # the distribution name, as `audit` reports it

#: Every module that declares part of this repo's vocabulary. A declarer missing here declares
#: nothing. ``tests/test_vocab.py`` checks that every declarer in ``domain/`` is listed.
_MODULES = (qptiff_vocab.DECLARED,)

#: What this module itself declares: the names that are not specific to one image format.
DECLARED = Declarer("vocab")


def _feature(name: str, dtype: Any, description: str) -> str:
    """Declare a feature and return its name."""
    return DECLARED.feature(name, dtype, description)


def _link(name: str, roi_type: str, description: str) -> str:
    """Declare a feature whose value is an ROI Record of ``roi_type``."""
    return DECLARED.link(name, roi_type, description)


def _record_type(name: str, description: str) -> str:
    """Declare a non-ROI Record type and return its name."""
    return DECLARED.record_type(name, description)


def _typed(name: str, record_type: str, description: str) -> str:
    """Declare a feature whose value is a Record of a declared non-ROI type."""
    return DECLARED.typed(name, record_type, description)


def _all() -> tuple[Declarer, ...]:
    """Every declarer, this module's own last."""
    return (*_MODULES, DECLARED)


def _declared() -> list[tuple[str, Any, str]]:
    """Every declared feature, across all modules."""
    return [f for d in _all() for f in d.features]


def _declared_roi_types() -> list[str]:
    return [t for d in _all() for t in d.roi_types]


def _declared_record_types() -> list[tuple[str, str]]:
    return [t for d in _all() for t in d.record_types]


# HITL review transport (general and core detection).
F_THUMBNAIL = _feature(
    "roi_thumbnail", ARTIFACT,
    "Clean 1:1 review image PNG (slide thumbnail or TMA crop); also the training image.")
F_PRED = _feature(
    "roi_pred", ARTIFACT,
    "Candidate detection GeoParquet, pre-review, in review px. The active-learning signal.")
F_CORRECTED = _feature(
    "roi_corrected_json", str,
    "The boxes a human accepted, JSON in review px, carrying rc + inferred for a dearrayed grid.")
F_MODIFIED = _feature(
    "roi_modified", bool,
    "True when the reviewer actually changed the candidate boxes. Gates training-data harvest.")
F_DET_OVERLAY = _feature(
    "roi_detection_overlay", ARTIFACT, "Detection boxes drawn on the review image (PNG).")
F_DEARRAY_OVERLAY = _feature(
    "roi_dearray_overlay", ARTIFACT,
    "The accepted dearray grid drawn on the TMA crop (PNG), the picture the reviewer approved.")
F_ROI_ACCEPTED = _feature(
    "roi_accepted", ARTIFACT, "Accepted general ROI GeoParquet (full-res px), on the WholeSlide.")
F_CORES_ACCEPTED = _feature(
    "cores_accepted", ARTIFACT, "Accepted core grid GeoParquet (full-res px), on the TMA.")
GENERAL_CONTEXT = _feature(
    "general_context", str,
    "Typed GeneralDetectContext as JSON (see contexts.py): raw + accepted state and their px space.")
CORE_CONTEXT = _feature(
    "core_context", str, "Typed CoreDetectContext as JSON (see contexts.py).")
DETECT_CONTEXT = _feature(
    "detect_context", ARTIFACT, "Durable context.json export of the typed detect context.")

# Imaging modality. Each image format states its own answer in its own module.
IMAGE_TYPE = _feature(
    "image_type", str,
    'Imaging modality: "multiplex" (bgnorm target), "h_and_e" (RGB brightfield), or "unknown".')
#: IMAGE_TYPE's values. H&E is RGB brightfield and skips bgnorm. Multiplex is many fluorescence
#: channels. check-qptiffs proposes one, mint-slides copies it to the WholeSlide, a human confirms.
H_AND_E = "h_and_e"
MULTIPLEX = "multiplex"
IMAGE_TYPE_UNKNOWN = "unknown"

# bgnorm: the result, and the runner's per-core bookkeeping.
BGNORMED = _feature(
    "bgnormed", ARTIFACT,
    "The bgnormed full-res FOV zarr. Written LAST, so its presence means this core is fully done.")
BGNORM_QC_FEATURE = _feature(
    "bgnorm_qc", ARTIFACT, "Per-core bgnorm QC metrics parquet, one row per channel.")
BGNORM_ATTEMPTS = _feature(
    "bgnorm_attempts", int, "Times the runner has launched a bgnorm-core for this core.")
BGNORM_CORE_FAILED = _feature(
    "bgnorm_core_failed", bool, "Exhausted the runner's resubmits; left for a manual re-run.")
BGNORM_SUBMIT_FAILURES = _feature(
    "bgnorm_submit_failures", int,
    "Runs that crashed before their sbatch landed, so they cost no compute and are refunded.")
BGNORM_LAST_CRASH_RUN = _feature(
    "bgnorm_last_crash_run", str,
    "Flow run id of the newest crash already accounted for, making the refund idempotent.")
BGNORM_DEFERRED = _feature(
    "bgnorm_deferred", bool,
    "Core left unprocessed when its run hit the time budget: relaunch without charging an attempt.")
# A TMA advances to bgnorm_pending on a partial result. This count shows the shortfall in review.
BGNORM_FAILED_CORES = _feature(
    "bgnorm_failed_cores", int, "Per-TMA: cores permanently given up on when it advanced.")
BGNORM_FAILED_CHANNELS = _feature(
    "bgnorm_failed_channels", str, "Channels a reviewer named as bad when failing a bgnorm gate.")

#: Every per-Core marker the runner writes. Reopening a core clears all of them, and every recovery
#: path clears this whole tuple.
BGNORM_BOOKKEEPING = (
    BGNORM_ATTEMPTS,
    BGNORM_CORE_FAILED,
    BGNORM_SUBMIT_FAILURES,
    BGNORM_LAST_CRASH_RUN,
    BGNORM_DEFERRED,
)

#: Per-TMA features that describe a bgnorm result. A reset that wipes a TMA's outputs clears these
#: too. The runner writes ``bgnorm_failed_cores``. A reviewer writes ``bgnorm_failed_channels``.
BGNORM_TMA_RESULT = (
    BGNORM_FAILED_CORES,
    BGNORM_FAILED_CHANNELS,
)

# Written by the persist stages.
# croi also declares the geometry features. The dtypes must match croi's exactly.
F_TMA = _link(
    "tma", "TMA", "The TMA ROI Record a Core belongs to (the parent link on a Core).")
PARQUET_ROW = _feature(
    "parquet_row", int,
    "Positional row of this ROI in its source GeoParquet. The pre-roi_id fallback; prefer roi_id.")
ROW = _feature("row", int, "Zero-based grid row of a Core within its TMA.")
COL = _feature("col", int, "Zero-based grid column of a Core within its TMA.")
IS_DETECTED = _feature(
    "is_detected", bool,
    "False for a grid cell inferred by the dearray fit rather than detected, so it has no real box.")
CORE_COMPLETENESS = _feature(
    "core_completeness", str, 'Whether a Core is "full" or "partial". A feature, not a type.')


def vocabulary():
    """Build the :class:`clinicalomx.vocab.Vocabulary` for this repo. Imports lamindb."""
    import lamindb as ln
    from clinicalomx.vocab import Vocabulary

    v = Vocabulary(OWNER)
    # Types first: ``Vocabulary.ensure`` resolves a dtype that names a type to that type's Record.
    for roi_type in _declared_roi_types():
        v.type(roi_type)
    # No parent: these stay outside the ROI umbrella. Never pass a `parent` here.
    for type_name, type_description in _declared_record_types():
        v.type(type_name, type_description)
    for name, dtype, description in _declared():
        v.feature(name, ln.Artifact if dtype is ARTIFACT else dtype, description)
    return v


_ENSURED = False  # process-level; the reconcile is idempotent


def ensure(*, dry_run: bool = False, force: bool = False):
    """Reconcile this repo's vocabulary against the connected instance. Idempotent.

    Returns the framework's ``Reconciler``, or ``None`` when skipped. Runs once per process unless
    ``force=True``. ``dry_run=True`` writes nothing.
    """
    global _ENSURED
    if _ENSURED and not (dry_run or force):
        return None
    rec = vocabulary().ensure(dry_run=dry_run)
    if not dry_run:
        _ENSURED = True
    return rec


def declared_names() -> tuple[str, ...]:
    """Every feature name this repo declares. Needs no database."""
    return tuple(name for name, _dtype, _desc in _declared())


def __getattr__(name: str):
    """Build ``VOCAB`` on first access (PEP 562), so importing this module never imports lamindb."""
    if name == "VOCAB":
        value = vocabulary()
        globals()["VOCAB"] = value  # cache: the indirection costs one lookup
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
