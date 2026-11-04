"""The foundation's catalogue of downstream products.

:mod:`clinicalomx.extract.docs` renders it into a CI-checked reference; regenerate that after a
change. Consumer-owned roles are bare strings. A consumer recipe of the same name, under the
``clinicalomx.recipes`` entry point, replaces one of these.
"""
from __future__ import annotations

from dataclasses import replace

from clinicalomx.extract import ANY_CHILD, Element, Recipe
from clinicalomx.names import FEATURES, TYPES

# Consumer-owned role names, declared in the repo that writes them.
BGNORMED = "bgnormed"                # segmentation / bgnormPy: background-normalised pixels
SEGMENTATIONS = "segmentations"      # segmentation: a label image
CELL_TABLES = "cell_tables"          # segmentation: per-cell measurements
CORE_SHAPES = "core_shapes"          # clinical_image_roi_object_detection: the core grid
CORES_ACCEPTED = "cores_accepted"    # qptiff_pipeline: the human-approved core grid
TMA_SHAPES = "tma_shapes"            # clinical_image_roi_object_detection: the TMA polygon
ROI_ACCEPTED = "roi_accepted"        # qptiff_pipeline: the human-approved general ROI
BGNORM_QC = "bgnorm_qc"              # qptiff_pipeline: per-core QC, one row per channel

#: What a Core placement reads: the dearray parquet and this core's row in it. Cores have no
#: ``bbox_*`` values.
_CORE_GEOMETRY = (FEATURES.SOURCE_ARTIFACT, "parquet_row")

#: What a TMA placement reads. The TMA detection objective writes these.
_TMA_GEOMETRY = ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")

_DEFAULT_BUILDERS = "clinicalomx.extract.builders"
#: Cores place from parquet geometry. TMAs and slides may name `coords:BboxPlacement`.
_DEFAULT_PLACEMENT = "clinicalomx.extract.coords:GeometryPlacement"

# Shared elements. A human-reviewed grid outranks a raw detection. Each image variant is its own
# element, so one object can hold raw beside background-normalised.
_CORE_RAW = Element(
    "raw", "image", roles=(FEATURES.SOURCE_IMAGE,), required=False,
    needs_features=_CORE_GEOMETRY,
    description="The unmodified qptiff pixels for this core's field of view.",
)
_CORE_BGNORM = Element(
    "bgnorm", "image", roles=(BGNORMED,), variable="adjusted_image", required=False,
    needs_features=_CORE_GEOMETRY,
    description="Background-normalised pixels.",
)
_CORE_BGNORM_Q = Element(
    "bgnormQ", "image", roles=(BGNORMED,), variable="adjusted_image_post_hoc", required=False,
    needs_features=_CORE_GEOMETRY,
    description="Background-normalised pixels after the post-hoc quantile adjustment.",
)
#: The image group for `requires_any`: a core with none of these blocks.
_CORE_IMAGES = (_CORE_RAW, _CORE_BGNORM, _CORE_BGNORM_Q)
_IMAGE_NAMES = tuple(e.name for e in _CORE_IMAGES)
_CORE_LABELS = Element(
    "labels", "labels", roles=(SEGMENTATIONS,), required=False,
    description="Cell label image, if a segmentation has been registered for this core.",
)
_CORE_TABLE = Element(
    "table", "table", roles=(CELL_TABLES,), required=False,
    # Declared, never invented: a wrong key is reported per table and per instance.
    instance_key="cell_id",
    description="Per-cell measurements for this core, region-keyed to the label image.",
)


def _per_child(element: Element) -> Element:
    """The same element, read from each descendant rather than from the anchor."""
    return replace(element, level="children")


# The catalogue. A core OME-TIFF has up to three series; availability decides which appear.
# `bgnorm` and `bgnormQ` are two variables of the same zarr.
OMETIFF_CORE = Recipe(
    name="ometiff.core", target="OME-TIFF",
    roi_types=(TYPES.CORE,), layout="single",
    elements=_CORE_IMAGES,
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_core",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "One core as a multi-series OME-TIFF - raw, background-normalised and post-hoc "
        "adjusted - with named, coloured channels. You get whichever series exist; all three "
        "cover the same field of view, so they can be compared pixel for pixel."),
)

# OME-TIFF above the core.
OMETIFF_TMA = Recipe(
    name="ometiff.tma", target="OME-TIFF",
    roi_types=(TYPES.TMA,), layout="single",
    elements=(
        Element("raw", "image", roles=(FEATURES.SOURCE_IMAGE,), required=False,
                needs_features=_TMA_GEOMETRY,
                description="The whole TMA's field, cropped from the slide pyramid."),
    ),
    requires_any=("raw",),
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_tma",
    #: TMAs have ``bbox_*`` values.
    placement="clinicalomx.extract.coords:BboxPlacement",
    description=(
        "A whole TMA as one OME-TIFF, cropped straight from the slide rather than assembled from "
        "cores. The fastest way to look at a TMA; use ometiff.tma.mosaic if you want only the "
        "cores, background-normalised."),
)

OMETIFF_TMA_MOSAIC = Recipe(
    name="ometiff.tma.mosaic", target="OME-TIFF",
    roi_types=(TYPES.TMA,), layout="merged", child_types=(TYPES.CORE,),
    # Composited: every core's `bgnormQ` becomes part of one series.
    aggregates=True,
    elements=tuple(_per_child(e) for e in _CORE_IMAGES),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_tma_mosaic",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "Every core of a TMA composited back into one image, each series a mosaic in the original "
        "slide's frame. Unlike spatialdata.tma.by-core, which keeps the cores as separate elements "
        "and lets a viewer place them, this places the pixels - but the gaps between the cores are "
        "written as ABSENT tiles and cost the file nothing, so the file is the cores and not the "
        "rectangle around them, and full resolution is the normal case. It is still a large file, "
        "bounded by the data; `downsample` is for an overview."),
)

OMEZARR_TMA_PLATE = Recipe(
    name="omezarr.tma.plate", target="OME-Zarr",
    roi_types=(TYPES.TMA,), layout="merged", child_types=(TYPES.CORE,),
    # Merged, but not composited: each core is its own image group in the plate.
    aggregates=False,
    elements=tuple(_per_child(e) for e in _CORE_IMAGES),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ngff:build_tma_plate",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "Every core of a TMA as one OME-NGFF plate: a well per grid position, an image group per "
        "series, and each one registered to the original slide by a coordinateTransformations "
        "translation rather than by pasting pixels. The same inputs as ometiff.tma.mosaic; the "
        "difference is that there is no canvas, so a full-resolution TMA costs the sum of its "
        "cores instead of the bounding box that contains them."),
)

OMETIFF_TMA_PER_CORE = Recipe(
    name="ometiff.tma.per-core", target="OME-TIFF",
    roi_types=(TYPES.TMA,), layout="per_child", child_types=(TYPES.CORE,),
    elements=_CORE_IMAGES,
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_core",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "One OME-TIFF per core in a TMA, each with whichever series that core has. The same "
        "inputs as ometiff.tma.mosaic; the difference is ninety-six files or one."),
)

SPATIALDATA_CORE = Recipe(
    name="spatialdata.core", target="SpatialData",
    roi_types=(TYPES.CORE,), layout="single",
    elements=(*_CORE_IMAGES, _CORE_LABELS, _CORE_TABLE),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.spatialdata:build_core",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "One core as a SpatialData. EVERY image variant that exists becomes its own element - "
        "raw beside background-normalised, in one coordinate system, so you can see what the "
        "normalisation did - plus labels and a table where they exist."),
)

SPATIALDATA_TMA_BY_CORE = Recipe(
    name="spatialdata.tma.by-core", target="SpatialData",
    roi_types=(TYPES.TMA,), layout="merged", child_types=(TYPES.CORE,),
    elements=(
        Element("cores", "shapes", roles=(CORES_ACCEPTED, CORE_SHAPES),
                description="The core grid for this TMA, human-approved where available."),
        *(_per_child(e) for e in _CORE_IMAGES),
        _per_child(_CORE_LABELS),
        _per_child(_CORE_TABLE),
        # A parquet with no geometry column is a `table`, so per-core bgnorm QC is one more row.
        _per_child(
            Element("qc", "table", roles=(BGNORM_QC,), required=False,
                    description="Per-channel bgnorm QC for this core: cohens_d, jsd, rho, "
                                "adjustment_scale, and whether the fit failed.")),
    ),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.spatialdata:build_merged",
    placement=_DEFAULT_PLACEMENT,
    publish_role="spatialdata",
    description=(
        "One master SpatialData for a whole TMA, with an element per core in one shared "
        "coordinate system. Anchors to the TMA, never to a Core."),
)

SPATIALDATA_TMA_PER_CORE = Recipe(
    name="spatialdata.tma.per-core", target="SpatialData",
    roi_types=(TYPES.TMA,), layout="per_child", child_types=(TYPES.CORE,),
    elements=(*_CORE_IMAGES, _CORE_LABELS, _CORE_TABLE),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.spatialdata:build_core",
    placement=_DEFAULT_PLACEMENT,
    description=(
        "A separate Core-level SpatialData for every core in a TMA. The same inputs as "
        "spatialdata.tma.by-core; the difference is one object or ninety-six."),
)

SPATIALDATA_SLIDE = Recipe(
    name="spatialdata.slide", target="SpatialData",
    roi_types=(TYPES.WHOLE_SLIDE,), layout="single",
    elements=(
        Element("image", "image", roles=(FEATURES.SOURCE_IMAGE,),
                description="The whole-slide pyramid."),
        Element("tma", "shapes", roles=(ROI_ACCEPTED, TMA_SHAPES), required=False,
                description="TMA polygons detected on this slide."),
    ),
    builder=f"{_DEFAULT_BUILDERS}.spatialdata:build_slide",
    placement=_DEFAULT_PLACEMENT,
    publish_role="spatialdata",
    description="A whole slide as a SpatialData: the pyramid plus the shapes found on it.",
)

#: The placement for each ROI subtype on a slide. An unlisted subtype is skipped and named.
_SPINE_PLACEMENT = {
    TYPES.CORE: _DEFAULT_PLACEMENT,
    TYPES.TILE: _DEFAULT_PLACEMENT,
    TYPES.TMA: "clinicalomx.extract.coords:BboxPlacement",
    TYPES.ROI: "clinicalomx.extract.coords:BboxPlacement",
}

SPATIALDATA_SLIDE_BY_ROI = Recipe(
    name="spatialdata.slide.by-roi", target="SpatialData",
    roi_types=(TYPES.WHOLE_SLIDE,), layout="merged",
    # Every subtype on the instance, at every level below the slide.
    child_types=(ANY_CHILD,), descend=True,
    elements=(
        Element("slide", "image", roles=(FEATURES.SOURCE_IMAGE,), required=False,
                description="The whole-slide pyramid, as the backdrop everything else sits on."),
        Element("rois", "shapes", roles=(ROI_ACCEPTED, TMA_SHAPES), required=False,
                description="The ROI polygons found on this slide."),
        *(_per_child(e) for e in _CORE_IMAGES),
        _per_child(_CORE_LABELS),
        _per_child(_CORE_TABLE),
    ),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.spatialdata:build_merged",
    placement=_SPINE_PLACEMENT,
    publish_role="spatialdata",
    description=(
        "One SpatialData for a whole slide, with an element per ROI BELOW it - every TMA, every "
        "core, and any other subtype the instance carries - in one shared coordinate system. "
        "Element keys are path-qualified (TMA0_Core_R0C0_bgnorm), because two TMAs on one slide "
        "both have an R0C0. Potentially thousands of elements; the plan says how many."),
)

# The same pixel products, anchored at the slide. Core geometry is already in slide pixels, so
# only the fan-out changes. Cores only, so a TMA's own field is not duplicated beside its cores.
_SLIDE_CORES = {"roi_types": (TYPES.WHOLE_SLIDE,), "child_types": (TYPES.CORE,),
                "descend": True,
                # Declared per subtype; an undeclared one is skipped and named.
                "placement": {TYPES.CORE: _DEFAULT_PLACEMENT}}

OMETIFF_SLIDE_MOSAIC = Recipe(
    name="ometiff.slide.mosaic", target="OME-TIFF",
    layout="merged", aggregates=True, **_SLIDE_CORES,
    elements=tuple(_per_child(e) for e in _CORE_IMAGES),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_tma_mosaic",
    description=(
        "Every core of every TMA on a slide composited into one image, each series a mosaic in "
        "the slide's own frame. The gaps between the TMAs, and the gaps inside them, are written "
        "as ABSENT tiles and cost the file nothing, so the file is the cores and not the "
        "whole-slide rectangle. It is still large, bounded by the data, and a slide-scale object "
        "can exceed the ceiling, which refuses with the number; `downsample` is what makes a "
        "whole-slide overview of the cores small."),
)

OMEZARR_SLIDE_PLATE = Recipe(
    name="omezarr.slide.plate", target="OME-Zarr",
    layout="merged", aggregates=False, **_SLIDE_CORES,
    elements=tuple(_per_child(e) for e in _CORE_IMAGES),
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ngff:build_slide_plate",
    description=(
        "Every core of every TMA on a slide as ONE OME-NGFF plate, each core registered to the "
        "slide by a coordinateTransformations translation rather than by pasting pixels. The rows "
        "are qualified by TMA (TMA0R0, TMA1R0), because two TMAs on one slide both have an R0C0. "
        "There is no canvas, so this costs the sum of the cores and nothing for the gap between "
        "the TMAs - which is what makes it the buildable form at full resolution."),
)

OMETIFF_SLIDE_PER_CORE = Recipe(
    name="ometiff.slide.per-core", target="OME-TIFF",
    layout="per_child", **_SLIDE_CORES,
    elements=_CORE_IMAGES,
    requires_any=_IMAGE_NAMES,
    builder=f"{_DEFAULT_BUILDERS}.ometiff:build_core",
    description=(
        "One OME-TIFF per core on the whole slide, each with whichever series that core has. The "
        "same files ometiff.tma.per-core writes, asked for once for a slide carrying four TMAs "
        "instead of four times."),
)

# `ometiff.tma` has no slide equivalent.

#: Rendered by ``clinicalomx-extract recipes`` and into the CI-checked reference. Consumer
#: recipes are excluded.
FOUNDATION_RECIPES: tuple[Recipe, ...] = (
    OMETIFF_CORE,
    OMETIFF_TMA,
    OMETIFF_TMA_MOSAIC,
    OMETIFF_TMA_PER_CORE,
    OMETIFF_SLIDE_MOSAIC,
    OMETIFF_SLIDE_PER_CORE,
    OMEZARR_TMA_PLATE,
    OMEZARR_SLIDE_PLATE,
    SPATIALDATA_CORE,
    SPATIALDATA_TMA_BY_CORE,
    SPATIALDATA_TMA_PER_CORE,
    SPATIALDATA_SLIDE,
    SPATIALDATA_SLIDE_BY_ROI,
)
