"""The element kind an artifact naturally is, checked against a recipe's declared kind.

A parquet with a geometry column is ``shapes``; one without is ``table``. The suffix decides first;
only a parquet's schema is read. The layer never acts on this; it reports disagreements.
"""
from __future__ import annotations

from clinicalomx.extract.builders import geo

#: Suffix -> kind, where the suffix settles it. `.zarr` has no kind: SpatialData, AnnData and label
#: images are all zarrs.
_BY_SUFFIX = {
    ".h5ad": "table",
    ".csv": "table",
    ".tif": "image",
    ".tiff": "image",
    ".qptiff": "image",
    ".png": "image",
    ".jpg": "image",
}

#: Variable names that mean a label image. Integer dtype alone does not.
_LABEL_VARIABLES = ("labels", "label", "segmentation", "masks", "mask")


def _suffix_of(path) -> str:
    name = str(path).rstrip("/").lower()
    for suffix in _BY_SUFFIX:
        if name.endswith(suffix):
            return suffix
    return ""


def natural_kind(artifact, *, variable: str | None = None) -> str | None:
    """The element kind this artifact naturally is, or ``None`` when it cannot be told."""
    # The variable name first: it is the one signal a zarr gives.
    if variable and any(variable.lower().startswith(v) for v in _LABEL_VARIABLES):
        return "labels"
    path = getattr(artifact, "path", artifact)
    suffix = _suffix_of(path)
    if not suffix or suffix == ".parquet":
        return None  # parquet is settled by its schema, in resolve_kind
    return _BY_SUFFIX.get(suffix)


def kind_of_parquet(path) -> str | None:
    """``shapes`` where the parquet carries geometry, ``table`` where it does not."""
    try:
        return "shapes" if geo.has_geometry(path) else "table"
    except Exception:  # noqa: BLE001 - unreadable here is "no opinion", not a fault
        return None


def resolve_kind(artifact, *, variable: str | None = None) -> str | None:
    """:func:`natural_kind`, consulting a parquet's schema where the suffix does not settle it."""
    path = getattr(artifact, "path", artifact)
    if str(path).rstrip("/").lower().endswith(".parquet"):
        return kind_of_parquet(path)
    return natural_kind(artifact, variable=variable)


#: The documented mapping, for the reference and the skill.
RULES: tuple[tuple[str, str], ...] = (
    ("parquet **with** a `geometry` column", "shapes"),
    ("parquet **without** one", "table"),
    ("`.h5ad`, `.csv`", "table"),
    ("`.tif`, `.tiff`, `.qptiff`, `.png`", "image"),
    ("`.zarr`", "_no opinion_ - a SpatialData, an AnnData and a label image are all zarrs"),
    ("a variable named `labels`/`mask`/`segmentation`", "labels"),
)
