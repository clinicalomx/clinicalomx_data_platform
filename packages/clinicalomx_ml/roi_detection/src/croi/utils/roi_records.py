"""The clinical-roi ROI Record schema: the type hierarchy, its features, and deterministic names."""
from __future__ import annotations

from typing import TYPE_CHECKING

import clinicalomx as cx
import lamindb as ln
from clinicalomx.names import FEATURES, TYPES

if TYPE_CHECKING:
    from clinicalomx.vocab import Vocabulary

TYPE_ROI = TYPES.ROI
TYPE_SLIDE = TYPES.WHOLE_SLIDE
TYPE_TMA = TYPES.TMA
TYPE_CORE = TYPES.CORE
_UMBRELLA_DESCRIPTION = "Region of interest. Base entity describing a spatial sampling."
_SUBTYPES = {
    TYPE_SLIDE: "A whole slide region in the image, or global ROI.",
    TYPE_TMA: "Tissue Microarray. Instance of an ROI.",
    TYPE_CORE: "Special instance of an ROI that describes a circular tissue core.",
}

PROVENANCE_FEATURES = {
    FEATURES.SOURCE_IMAGE: ln.Artifact, # the source QPTIFF
    FEATURES.SOURCE_ARTIFACT: ln.Artifact, # the GeoParquet holding the geometry
}


def ensure_roi_types() -> None:
    """Create the project's ROI type hierarchy if missing."""
    cx.ensure_types(
        _SUBTYPES, 
        umbrella=TYPE_ROI, 
        umbrella_description=_UMBRELLA_DESCRIPTION
    )


def ensure_roi_features(vocabulary: Vocabulary, roi_scalars: dict[str, type]) -> None:
    """Create every Feature an objective writes. Idempotent.

    ``vocabulary`` and ``roi_scalars`` are the objective's ``vocab.VOCAB`` and ``vocab.ROI_SCALARS``.
    """
    vocabulary.ensure()
    cx.ensure_features({**roi_scalars, **PROVENANCE_FEATURES})


def get_or_create_roi(name: str, roi_type: str, **feature_vals):
    """Get-or-create the ROI Record ``name`` of subtype ``roi_type``."""
    return cx.get_or_create(name, roi_type, features=feature_vals)


# --- deterministic naming (must match the existing instance) -----------
def slide_name(stem: str) -> str:
    """WholeSlide ROI name: the unsanitized slide stem."""
    return str(stem)


def tma_name(slide: str, t: int) -> str:
    """TMA ROI name, e.g. ``<slide>:TMA0``."""
    return f"{slide}:TMA{t}"


def core_name(slide: str, t: int, r: int, c: int) -> str:
    """Core ROI name from grid coords, e.g. ``<slide>:TMA0:R0C0`` (matches dearray)."""
    return f"{slide}:TMA{t}:R{r}C{c}"
