"""Dearray a detected TMA into per-core shapes.

Runs the core detector, fits the grid (`gridding.py`), infers missing cells and writes detected
and inferred cores onto a spatialdata object as a shapes element. Only ``dearray_sdata`` loads a
champion.
"""

from __future__ import annotations

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.geometry import box
from spatialdata.models import ShapesModel
from spatialdata.transformations import get_transformation

from croi.detection.detector import suppress_overlaps

from croi.objectives.tma_core.gridding import estimate_grid, missing_cell_boxes, plot_grid
from croi.utils.image_preprocess import (
    get_smallest_scale_image,
    max_intensity_projection,
    image_to_8bit,
    grayscale_to_rgb
)


def detect_cores(
    xarray_image,
    detector,
    detection_bounding_box=None,
    *,
    conf: float = 0.25,
    imgsz: int = 640,
    nms_iou: float = 0.6,
    contain_ios: float = 0.9,
    agnostic: bool = True,
):
    """Run ``detector`` on an image and apply class-agnostic overlap suppression to the raw boxes.

    Build the detector with ``contained_thr: 0``: suppression happens here, once.
    ``conf`` filters at predict time; ``nms_iou``, ``contain_ios`` and ``agnostic`` set the
    suppression. Returns a GeoDataFrame with ``class``, ``conf`` and ``geometry``.
    """
    dets = suppress_overlaps(
        detector.predict(xarray_image, conf=conf, imgsz=imgsz),
        iou_thr=nms_iou,
        ios_thr=contain_ios,
        class_agnostic=agnostic,
    )

    records = [
        {"class": d.class_name, "conf": d.confidence, "geometry": box(*d.xyxy)}
        for d in dets
    ]
    if not records:  # an empty but well-formed gdf
        return gpd.GeoDataFrame(
            {"class": [], "conf": [], "geometry": []}, geometry="geometry", crs=None
        )
    return gpd.GeoDataFrame(records, geometry="geometry", crs=None)

def plot_cores(gdf, xarray_image=None, ax=None):
    if ax is None:
        fig, ax = plt.subplots(figsize=(10, 10))
    if xarray_image is not None:
        ax.imshow(xarray_image)
    color_map = {"full_core": "blue", "partial_core": "red"}

    gdf.plot(ax=ax, facecolor="none", edgecolor="white", linewidth=1.5)

    for _, row in gdf.iterrows():
        minx, miny, _, _ = row.geometry.bounds
        c = color_map.get(row["class"], "white")
        ax.text(minx, miny - 4, f"{row['class']} {row['conf']:.2f}",
                color=c, fontsize=6, va="bottom",
                bbox=dict(facecolor="black", alpha=0.1, pad=1, edgecolor="none"))
    
    if ax is None:
        plt.tight_layout()
        plt.show()

def detect_and_plot_cores(xarray_image, detector, **kwargs):
    gdf = detect_cores(xarray_image, detector, **kwargs)
    plot_cores(xarray_image, gdf)
    return gdf

def dearray_sdata(
    sdata,
    image_name,
    save_shapes=False,
    expected_shape=None,
    shapes_name="tma_cores",
):
    img = get_smallest_scale_image(sdata.images[image_name])
    img_transforms = get_transformation(img, get_all=True)
    img = max_intensity_projection(img)
    img = img.data.compute()
    img = image_to_8bit(img)
    img = grayscale_to_rgb(img)

    # contained_thr 0: detect_cores owns suppression.
    from croi.objectives.tma_core.registry import load_champion

    detector = load_champion("production", {"contained_thr": 0})

    cores = detect_and_plot_cores(img, detector, conf=0.25)

    grid = estimate_grid(cores, expected_shape=expected_shape)

    fig, ax = plt.subplots(figsize=(14, 14))
    plot_grid(grid, gdf=cores, image=img, ax=ax, show_indices=True)
    plt.tight_layout()
    plt.show()

    inferred = missing_cell_boxes(grid)
    print(f"inferred {len(inferred)} missing cells")

    detected = cores.copy()
    rows, cols = np.full(len(detected), -1), np.full(len(detected), -1)
    for r in range(grid.n_rows):
        for c in range(grid.n_cols):
            idx = grid.assignment[r, c]
            if idx >= 0:
                rows[idx] = r
                cols[idx] = c
    detected["row"] = rows
    detected["col"] = cols
    all_cells = gpd.GeoDataFrame(
        pd.concat([detected, inferred], ignore_index=True),
        geometry="geometry",
    )
    print(f"total cells (detected + inferred): {len(all_cells)}")

    all_cells_parsed = ShapesModel.parse(
        all_cells,
        transformations=img_transforms,
    )

    sdata.shapes[shapes_name] = all_cells_parsed

    if save_shapes:
        all_cells_parsed.write_element(shapes_name)

    return sdata