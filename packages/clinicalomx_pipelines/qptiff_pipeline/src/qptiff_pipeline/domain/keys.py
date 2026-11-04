"""Lamindb Artifact keys: the on-disk layout under the Storage root.

    clinicalomx-qptiff-pipeline/   # PIPELINE_ROOT
      <slide>/                     # slug of the qptiff FILENAME incl. suffix, e.g. foo-bar-qptiff
      predictions/general/       thumbnail.png  detections.png  rois_pred.parquet
      rois/                      rois.parquet
      training/general/          labels.parquet
      <tma>/                     # e.g. tma0
        predictions/core/        thumbnail.png  detections.png  cores_pred.parquet
        cores/                   cores.parquet  dearray.png
        <core>/                  # e.g. r4c2: every derivative of ONE core
          bgnormed-image-<runid>.zarr    bgnorm-qc-<runid>.parquet

All keys are relative to the Storage root (the flows' ``target_directory``), lowercase and
hyphen-separated. Record names encode the hierarchy as ``<slide-stem>:TMA<r>:R<r>C<c>``.

The raw ``.qptiff`` is registered in place and never moved, so it has no key here.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# Every derivative of this repo nests under this folder.
PIPELINE_ROOT = "clinicalomx-qptiff-pipeline"

# Subpath (namespace folder + filename) per artifact kind, relative to the slide or TMA folder.
GENERAL = {
    "thumbnail": "predictions/general/thumbnail.png",
    "detections": "predictions/general/detections.png",
    "pred": "predictions/general/rois_pred.parquet",
    "rois": "rois/rois.parquet",
    # The typed detect context (contexts.py). Not model-versioned: it sits outside predictions/.
    "context": "context.json",
}
CORE = {
    "thumbnail": "predictions/core/thumbnail.png",
    "detections": "predictions/core/detections.png",
    "pred": "predictions/core/cores_pred.parquet",
    "context": "context.json",  # see GENERAL["context"]
    "cores": "cores/cores.parquet",
    "dearray": "cores/dearray.png",
}


def slug(s: object) -> str:
    """Filesystem-safe slug: lowercase, non-alphanumerics collapsed to single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def slide_dir(qptiff_key: str) -> str:
    """Slide folder: ``<PIPELINE_ROOT>/<slug of the qptiff filename, suffix included>``.

    ``dir/Foo 1.qptiff`` -> ``clinicalomx-qptiff-pipeline/foo-1-qptiff``.
    """
    return f"{PIPELINE_ROOT}/{slug(PurePosixPath(str(qptiff_key)).name)}"


def _segments(record_name: str) -> list[str]:
    """Record name split into its hierarchy parts: 'base:TMA0:R4C2' -> ['base','TMA0','R4C2']."""
    return str(record_name).split(":")


def tma_dir(qptiff_key: str, tma_name: str) -> str:
    """Folder for one TMA under its slide: ``<slide>/<tma>`` (e.g. ``...-qptiff/tma0``)."""
    return f"{slide_dir(qptiff_key)}/{slug(_segments(tma_name)[1])}"


def general_key(qptiff_key: str, kind: str, model_uid: str | None = None) -> str:
    """Key for a per-slide artifact (kind in GENERAL): ``<slide>/<GENERAL[kind]>``.

    ``model_uid`` nests prediction outputs under ``predictions/general-<model_uid>/``.
    """
    sub = GENERAL[kind]
    if model_uid and sub.startswith("predictions/general"):
        sub = sub.replace("predictions/general", f"predictions/general-{model_uid}", 1)
    return f"{slide_dir(qptiff_key)}/{sub}"


def core_key(qptiff_key: str, tma_name: str, kind: str, model_uid: str | None = None) -> str:
    """Key for a per-TMA core artifact (kind in CORE): ``<slide>/<tma>/<CORE[kind]>``.

    ``model_uid`` nests prediction outputs under ``predictions/core-<model_uid>/``.
    """
    sub = CORE[kind]
    if model_uid and sub.startswith("predictions/core"):
        sub = sub.replace("predictions/core", f"predictions/core-{model_uid}", 1)
    return f"{tma_dir(qptiff_key, tma_name)}/{sub}"


def general_accepted_key(qptiff_key: str, run_uid: str) -> str:
    """Key for accepted general ROIs: ``<slide>/accepted-<run_uid>/rois.parquet``.

    Each accept writes a new key. The slide's ``roi_accepted`` points at the latest.
    """
    return f"{slide_dir(qptiff_key)}/accepted-{run_uid}/rois.parquet"


def core_accepted_key(qptiff_key: str, tma_name: str, run_uid: str, kind: str = "cores") -> str:
    """Key for an accepted core artifact (kind ``cores`` or ``dearray``).

    ``<slide>/<tma>/accepted-<run_uid>/<cores.parquet|dearray.png>``.
    """
    leaf = {"cores": "cores.parquet", "dearray": "dearray.png"}[kind]
    return f"{tma_dir(qptiff_key, tma_name)}/accepted-{run_uid}/{leaf}"


def qptiff_thumbnail_key(qptiff_key: str) -> str:
    """Key for the qptiff's embedded thumbnail, saved as PNG: ``<slide>/qptiff/thumbnail.png``."""
    return f"{slide_dir(qptiff_key)}/qptiff/thumbnail.png"


def core_dir(qptiff_key: str, core_name: str) -> str:
    """Folder for one Core under its TMA: ``<slide>/<tma>/<core>``."""
    segs = _segments(core_name)  # [base, TMA0, R0C7]
    tma_name = ":".join(segs[:2])
    return f"{tma_dir(qptiff_key, tma_name)}/{slug(segs[2])}"


def bgnorm_key(qptiff_key: str, core_name: str, run_id: str) -> str:
    """Key for a per-core bgnorm zarr: ``<slide>/<tma>/<core>/bgnormed-image-<run_id>.zarr``.

    Run-scoped: a re-run writes a new zarr and keeps the old one. A flow retry reuses the run id.
    """
    return f"{core_dir(qptiff_key, core_name)}/bgnormed-image-{run_id}.zarr"


def bgnorm_qc_key(qptiff_key: str, core_name: str, run_id: str) -> str:
    """Key for a per-core bgnorm QC table, beside that run's zarr: ``bgnorm-qc-<run_id>.parquet``.

    One row per channel, holding the per-channel GMM summaries from ``bgnorm.bgnorm_pixels``.
    """
    return f"{core_dir(qptiff_key, core_name)}/bgnorm-qc-{run_id}.parquet"


def bgnorm_experiment_name(core_name: str, *, prefix: str = "bgnorm") -> str:
    """Per-core MLflow experiment name: ``<prefix>/<core name with ':' replaced by '_'>``.

    The channels are the runs. The core segment must not contain ``/``: MLflow shows it as a folder.
    """
    return f"{prefix}/{core_name.replace(':', '_')}"
