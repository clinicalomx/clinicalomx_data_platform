"""TMA grid estimation from detected core polygons.

A copy of ``croi.objectives.tma_core.gridding`` for the light review image, which cannot install
croi. Keep it in sync with that module. ``review.app._dearray`` prefers the croi import.

Estimates the regular grid even when cores are missing, the grid is rotated, or detections are
partial (after Bahlmann et al. 2011 and ATMAD, Robinson et al. 2018):

  1. Pull centroids and sizes from the GeoDataFrame.
  2. Estimate the rotation from Delaunay edge orientations.
  3. Derotate the centroids so the grid axes align with x and y.
  4. Fit a 1D lattice along each axis: (offset, spacing, n_clusters).
  5. Rebuild cell centres in the image frame.
  6. Assign each detection to its nearest cell. Empty cells are missing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import find_peaks
from scipy.spatial import Delaunay


@dataclass
class TMAGrid:
    """Estimated TMA grid plus the assignment of detections to cells.

    Coordinates are image pixels, top-left origin, y down. Cell (i, j) centre is
    ``origin + i * row_vec + j * col_vec``. ``angle_rad`` is in [-pi/4, pi/4). ``assignment`` holds
    the input row index per cell, -1 if missing. ``residuals`` is NaN for unassigned detections.
    """

    origin: np.ndarray
    row_vec: np.ndarray
    col_vec: np.ndarray
    n_rows: int
    n_cols: int
    angle_rad: float
    cell_size: np.ndarray  # (w, h)
    assignment: np.ndarray  # (n_rows, n_cols), int, -1 = missing
    residuals: np.ndarray   # (n_detections,)
    source_gdf: object = None  # input GeoDataFrame (real/detected geoms)

    @property
    def cell_centers(self) -> np.ndarray:
        """(n_rows, n_cols, 2) array of predicted cell centers in image px."""
        ii, jj = np.mgrid[: self.n_rows, : self.n_cols]
        return (
            self.origin
            + ii[..., None] * self.row_vec
            + jj[..., None] * self.col_vec
        )

    @property
    def missing_mask(self) -> np.ndarray:
        """Boolean (n_rows, n_cols) - True where no detection landed."""
        return self.assignment < 0

    @property
    def occupancy(self) -> float:
        """Fraction of cells that have a detection assigned."""
        return float((~self.missing_mask).mean())

    def cell_xy(self, row: int, col: int) -> np.ndarray:
        """Predicted (x, y) center of cell (row, col)."""
        return self.origin + row * self.row_vec + col * self.col_vec

    def ideal_box(self, row: int, col: int):
        """Axis-aligned fit box of `cell_size` centered on cell (row, col)."""
        from shapely.geometry import box

        cx, cy = self.cell_xy(row, col)
        w, h = self.cell_size
        return box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def to_geodataframe(self, source_gdf=None):
        """One row per grid cell as a GeoDataFrame.

        Columns: ``row``, ``col``, ``occupied``, ``detection_idx`` (-1 if missing), ``conf`` (-1 if
        missing), ``geometry`` (the detected box, ``None`` if missing) and ``ideal_geometry`` (the
        fit box of ``cell_size`` at the lattice point). ``source_gdf`` overrides
        ``self.source_gdf``.
        """
        import geopandas as gpd

        src = source_gdf if source_gdf is not None else self.source_gdf
        records = []
        geoms = []
        ideal_geoms = []
        for i in range(self.n_rows):
            for j in range(self.n_cols):
                idx = int(self.assignment[i, j])
                occupied = idx != -1
                ideal = self.ideal_box(i, j)
                if occupied and src is not None:
                    geom = src.geometry.iloc[idx]
                    cls = src["class"].iloc[idx] if "class" in src else "core"
                    conf = float(src["conf"].iloc[idx]) if "conf" in src else -1.0
                else:
                    geom = None
                    cls = "core" if occupied else "inferred_core"
                    conf = -1.0
                records.append(
                    {
                        "row": i,
                        "col": j,
                        "occupied": occupied,
                        "detection_idx": idx,
                        "class": cls,
                        "conf": conf,
                    }
                )
                geoms.append(geom)
                ideal_geoms.append(ideal)
        crs = src.crs if src is not None else None
        gdf = gpd.GeoDataFrame(records, crs=crs)
        gdf["geometry"] = gpd.GeoSeries(geoms, crs=crs)
        gdf["ideal_geometry"] = gpd.GeoSeries(ideal_geoms, crs=crs)
        return gdf.set_geometry("geometry")


# Step 1: extract centroids and sizes


def _centroids_and_sizes(gdf) -> tuple[np.ndarray, np.ndarray]:
    """Pull (N, 2) centroid array and (N, 2) (width, height) array from a gdf."""
    if len(gdf) == 0:
        return np.zeros((0, 2)), np.zeros((0, 2))
    bounds = gdf.geometry.bounds.to_numpy()  # minx, miny, maxx, maxy
    cx = 0.5 * (bounds[:, 0] + bounds[:, 2])
    cy = 0.5 * (bounds[:, 1] + bounds[:, 3])
    w = bounds[:, 2] - bounds[:, 0]
    h = bounds[:, 3] - bounds[:, 1]
    return np.column_stack([cx, cy]), np.column_stack([w, h])


# Step 2: angle estimation


def estimate_grid_angle(
    centroids: np.ndarray,
    length_quantile: float = 0.66,
    max_iter: int = 3,
) -> float:
    """Estimate the grid rotation angle from the centroid layout. Returns an angle in [-pi/4, pi/4).

    Takes the circular mean, modulo pi/2, of the shortest ``length_quantile`` Delaunay edges:
    ``angle(mean(exp(4i * phi))) / 4``. A few iterations keep only edges near a grid axis.
    """
    if len(centroids) < 3:
        return 0.0

    tri = Delaunay(centroids)
    edge_set: set[tuple[int, int]] = set()
    for simplex in tri.simplices:
        a, b, c = simplex
        edge_set.add((min(a, b), max(a, b)))
        edge_set.add((min(b, c), max(b, c)))
        edge_set.add((min(a, c), max(a, c)))
    edges = np.array(sorted(edge_set))
    p1 = centroids[edges[:, 0]]
    p2 = centroids[edges[:, 1]]
    vec = p2 - p1
    lengths = np.linalg.norm(vec, axis=1)
    angles = np.arctan2(vec[:, 1], vec[:, 0])  # (-pi, pi]

    # Initial filter: keep shortest length_quantile fraction.
    cutoff = np.quantile(lengths, length_quantile)
    keep = lengths <= cutoff
    weights = 1.0 / np.maximum(lengths[keep], 1e-6)
    phi = angles[keep]

    # Circular mean modulo pi/2.
    z = np.exp(4j * phi)
    mean_z = np.sum(z * weights) / np.sum(weights)
    theta = float(np.angle(mean_z)) / 4.0
    theta = _wrap_to_quarter(theta)

    # Iterate: keep only edges within ~15 degrees of {theta, theta+pi/2}.
    for _ in range(max_iter):
        # Signed deviation from the nearest grid axis, in [-pi/4, pi/4].
        dev = _fold_to_quarter(angles - theta)
        keep = (np.abs(dev) < np.deg2rad(15.0)) & (lengths <= cutoff)
        if keep.sum() < 6:
            break
        w = 1.0 / np.maximum(lengths[keep], 1e-6)
        z = np.exp(4j * angles[keep])
        mean_z = np.sum(z * w) / np.sum(w)
        new_theta = _wrap_to_quarter(float(np.angle(mean_z)) / 4.0)
        if abs(new_theta - theta) < 1e-5:
            theta = new_theta
            break
        theta = new_theta

    return theta


def _wrap_to_quarter(theta: float) -> float:
    """Map an angle into [-pi/4, pi/4)."""
    half_pi = np.pi / 2.0
    theta = (theta + np.pi / 4.0) % half_pi - np.pi / 4.0
    return float(theta)


def _fold_to_quarter(theta: np.ndarray) -> np.ndarray:
    """Vectorised wrap into [-pi/4, pi/4)."""
    half_pi = np.pi / 2.0
    return (theta + np.pi / 4.0) % half_pi - np.pi / 4.0


# Step 3: derotate


def _rotation_matrix(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s], [s, c]])


def derotate(points: np.ndarray, angle: float) -> np.ndarray:
    """Rotate `points` by -angle around the origin.  Shape: (N, 2)."""
    return points @ _rotation_matrix(-angle).T


def estimate_axis_spacings(
    centroids_derot: np.ndarray,
    max_skew_ratio: float = 0.35,
) -> tuple[float | None, float | None]:
    """Estimate (dx, dy) grid pitch from Delaunay edges in the derotated frame.

    Keeps near-axis edges and takes the median of the lower half of their lengths, which excludes
    edges that skip missing cores.
    """
    if len(centroids_derot) < 3:
        return None, None
    tri = Delaunay(centroids_derot)
    edge_set: set[tuple[int, int]] = set()
    for simplex in tri.simplices:
        a, b, c = simplex
        edge_set.add((min(a, b), max(a, b)))
        edge_set.add((min(b, c), max(b, c)))
        edge_set.add((min(a, c), max(a, c)))
    edges = np.array(sorted(edge_set))
    vec = centroids_derot[edges[:, 1]] - centroids_derot[edges[:, 0]]
    abs_vec = np.abs(vec)

    def _pitch(on_axis, off_axis):
        mask = (off_axis < max_skew_ratio * on_axis) & (on_axis > 1e-3)
        if mask.sum() == 0:
            return None
        vals = on_axis[mask]
        cutoff = np.quantile(vals, 0.5)
        return float(np.median(vals[vals <= cutoff]))

    dx = _pitch(abs_vec[:, 0], abs_vec[:, 1])
    dy = _pitch(abs_vec[:, 1], abs_vec[:, 0])
    return dx, dy


# Step 4: 1D lattice fit


def fit_lattice_1d(
    coords: np.ndarray,
    expected_n: int | None = None,
    init_spacing: float | None = None,
    kde_bandwidth_factor: float = 0.20,
) -> tuple[float, float, int, np.ndarray]:
    """Fit a 1D regular lattice to scattered coordinates.

    KDE-smooths the coordinates and finds peaks, estimates the spacing from the peak gaps, then
    rounds each peak to an index and refits offset and spacing by least squares.

    Parameters
    ----------
    coords:
        1D coordinates (e.g. derotated x-values).
    expected_n:
        Optional expected number of sites. Sets the KDE bandwidth and keeps that many peaks.
    init_spacing:
        Optional initial spacing estimate.
    kde_bandwidth_factor:
        KDE bandwidth as a fraction of the spacing estimate.

    Returns
    -------
    offset, spacing, n_sites, peak_indices
        The i-th peak lies near ``offset + peak_indices[i] * spacing``. ``n_sites`` counts missing
        sites too.
    """
    coords = np.asarray(coords, dtype=float).ravel()
    if len(coords) == 0:
        return 0.0, 1.0, 0, np.array([], dtype=int)
    if len(coords) == 1:
        return float(coords[0]), 1.0, 1, np.array([0])

    span = float(coords.max() - coords.min())

    # Step A: rough spacing prior for the KDE bandwidth.
    if init_spacing is not None:
        rough_spacing = float(init_spacing)
    elif expected_n is not None and expected_n > 1:
        rough_spacing = span / (expected_n - 1)
    else:
        diffs = np.diff(np.sort(coords))
        diffs = diffs[diffs > 0]
        rough_spacing = float(np.median(diffs)) if len(diffs) else max(span, 1.0)

    bw = max(rough_spacing * kde_bandwidth_factor, 1e-3)

    # Step B: KDE and peak detection.
    pad = 3 * bw
    grid = np.linspace(coords.min() - pad, coords.max() + pad, 2000)
    # vectorised Gaussian KDE
    diff = (grid[:, None] - coords[None, :]) / bw
    kde = np.exp(-0.5 * diff * diff).sum(axis=1)
    min_distance = max(int(0.5 * rough_spacing / (grid[1] - grid[0])), 2)
    peak_ix, _props = find_peaks(kde, distance=min_distance)

    if expected_n is not None and len(peak_ix) > expected_n:
        order = np.argsort(kde[peak_ix])[-expected_n:]
        peak_ix = np.sort(peak_ix[order])

    peaks = grid[peak_ix]

    if len(peaks) < 2:
        # One cluster only: use the rough spacing prior.
        offset = float(peaks[0]) if len(peaks) else float(coords.mean())
        return offset, rough_spacing, max(1, len(peaks)), np.array([0] * len(peaks))

    # Step C: indices by rounding, then a linear refit.
    init_offset = float(peaks[0])
    spacing = float(np.median(np.diff(peaks)))
    offset = init_offset
    indices = np.zeros(len(peaks), dtype=int)
    for _ in range(20):
        indices = np.round((peaks - offset) / spacing).astype(int)
        if len(np.unique(indices)) < 2:
            break
        A = np.column_stack([indices.astype(float), np.ones(len(indices))])
        sol, *_ = np.linalg.lstsq(A, peaks, rcond=None)
        new_spacing, new_offset = float(sol[0]), float(sol[1])
        if (
            abs(new_offset - offset) < 1e-4
            and abs(new_spacing - spacing) < 1e-4
        ):
            offset, spacing = new_offset, new_spacing
            break
        offset, spacing = new_offset, new_spacing

    # Normalise so smallest index is 0.
    base = int(indices.min())
    indices = indices - base
    offset = offset + base * spacing
    n_sites = int(indices.max() + 1)
    return offset, spacing, n_sites, indices


# Step 5: assignment


def _assign_detections(
    centroids: np.ndarray,
    cell_centers: np.ndarray,  # (n_rows, n_cols, 2)
    confidences: np.ndarray | None,
    max_dist: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Assign each detection to its nearest cell, at most one per cell.

    Ties go to the higher-confidence detection, or the closer one without confidences.

    Returns
    -------
    assignment: (n_rows, n_cols) int array, -1 for empty cells.
    residuals:  (N,) distance to the assigned cell. NaN for unused detections.
    """
    n_rows, n_cols = cell_centers.shape[:2]
    flat_centers = cell_centers.reshape(-1, 2)

    # Pairwise distances.
    diffs = centroids[:, None, :] - flat_centers[None, :, :]
    d2 = np.sum(diffs * diffs, axis=2)
    nearest = np.argmin(d2, axis=1)
    dist = np.sqrt(d2[np.arange(len(centroids)), nearest])

    valid = dist <= max_dist
    # Order candidates: high-confidence first, then small distance.
    if confidences is not None:
        score = -confidences.astype(float)
    else:
        score = dist.copy()
    order = np.argsort(score)

    assignment = -np.ones((n_rows, n_cols), dtype=int)
    used = np.zeros_like(dist, dtype=bool)
    for det_idx in order:
        if not valid[det_idx]:
            continue
        flat = nearest[det_idx]
        r, c = divmod(int(flat), n_cols)
        if assignment[r, c] == -1:
            assignment[r, c] = det_idx
            used[det_idx] = True

    residuals = np.where(used, dist, np.nan)
    return assignment, residuals


# Step 6: entry point


def estimate_grid(
    gdf,
    expected_shape: tuple[int, int] | None = None,
    confidence_col: str = "conf",
    refine: bool = True,
    inlier_factor: float = 0.4,
    assign_factor: float = 0.5,
) -> TMAGrid:
    """Estimate a TMA grid from a GeoDataFrame of detected cores.

    Parameters
    ----------
    gdf:
        GeoDataFrame from the core detector, one detected core per row, with a ``geometry`` column.
    expected_shape:
        Optional ``(n_rows, n_cols)`` prior for the lattice fits and orientation.
    confidence_col:
        Column used to break ties between detections for one cell. Ignored if missing.
    refine:
        If True, fit once, drop detections beyond ``inlier_factor * spacing``, and refit.
    inlier_factor:
        Residual threshold for the refine pass, as a fraction of the mean spacing.
    assign_factor:
        Residual threshold for final assignment, as a fraction of the mean spacing. Detections
        beyond it are excluded.
    """
    centroids, sizes = _centroids_and_sizes(gdf)
    if len(centroids) < 4:
        raise ValueError(
            f"need at least 4 detections to estimate a grid, got {len(centroids)}"
        )

    confidences = (
        gdf[confidence_col].to_numpy() if confidence_col in gdf.columns else None
    )

    grid = _fit_once(centroids, sizes, confidences, expected_shape, assign_factor)

    if refine:
        spacing = 0.5 * (
            np.linalg.norm(grid.row_vec) + np.linalg.norm(grid.col_vec)
        )
        inlier_mask = np.isfinite(grid.residuals) & (
            grid.residuals <= inlier_factor * spacing
        )
        if inlier_mask.sum() >= 4 and inlier_mask.sum() < len(centroids):
            inlier_conf = confidences[inlier_mask] if confidences is not None else None
            grid = _fit_once(
                centroids[inlier_mask],
                sizes[inlier_mask],
                inlier_conf,
                expected_shape,
                assign_factor,
                # Assignment and residuals cover all detections: re-assigned below.
            )
            # Re-assign on the full detection set against the refined grid.
            cell_centers = grid.cell_centers
            max_dist = assign_factor * spacing
            assignment, residuals = _assign_detections(
                centroids, cell_centers, confidences, max_dist
            )
            grid.assignment = assignment
            grid.residuals = residuals

    grid.source_gdf = gdf
    return grid


def _fit_once(
    centroids: np.ndarray,
    sizes: np.ndarray,
    confidences: np.ndarray | None,
    expected_shape: tuple[int, int] | None,
    assign_factor: float,
) -> TMAGrid:
    """One pass: angle -> derotate -> 1D fits -> reconstruct -> assign."""
    angle = estimate_grid_angle(centroids)
    derot = derotate(centroids, angle)

    # With a shape prior, the axis with the larger span gets the larger count.
    if expected_shape is not None:
        n_rows_exp, n_cols_exp = expected_shape
        span_x = float(derot[:, 0].max() - derot[:, 0].min())
        span_y = float(derot[:, 1].max() - derot[:, 1].min())
        if (span_x >= span_y) == (n_cols_exp >= n_rows_exp):
            expect_x, expect_y = n_cols_exp, n_rows_exp
        else:
            expect_x, expect_y = n_rows_exp, n_cols_exp
    else:
        expect_x = expect_y = None

    # Axis spacings from short Delaunay edges, for the KDE bandwidth.
    dx_init, dy_init = estimate_axis_spacings(derot)
    off_x, dx, n_x, _ = fit_lattice_1d(
        derot[:, 0], expected_n=expect_x, init_spacing=dx_init,
    )
    off_y, dy, n_y, _ = fit_lattice_1d(
        derot[:, 1], expected_n=expect_y, init_spacing=dy_init,
    )

    # Rotate the (offset, dx, dy) back into image-space basis vectors.
    R = _rotation_matrix(angle)
    origin_derot = np.array([off_x, off_y])
    origin = R @ origin_derot
    col_vec = R @ np.array([dx, 0.0])
    row_vec = R @ np.array([0.0, dy])

    # Rows are the y axis, columns the x axis.
    n_rows, n_cols = n_y, n_x

    # Typical cell size: the median detected box, used for missing cells.
    if len(sizes):
        cell_size = np.median(sizes, axis=0)
    else:
        cell_size = np.array([abs(dx), abs(dy)])

    # Build cell centers and assign detections.
    ii, jj = np.mgrid[:n_rows, :n_cols]
    cell_centers = origin + ii[..., None] * row_vec + jj[..., None] * col_vec

    spacing = 0.5 * (abs(dx) + abs(dy))
    max_dist = assign_factor * spacing
    assignment, residuals = _assign_detections(
        centroids, cell_centers, confidences, max_dist
    )

    return TMAGrid(
        origin=origin,
        row_vec=row_vec,
        col_vec=col_vec,
        n_rows=n_rows,
        n_cols=n_cols,
        angle_rad=angle,
        cell_size=cell_size,
        assignment=assignment,
        residuals=residuals,
    )


def plot_grid(
    grid: TMAGrid,
    gdf=None,
    image=None,
    ax=None,
    show_indices: bool = True,
    show_lattice: bool = True,
    show_delaunay: bool = False,
    show_grid_edges: bool = False,
    show_cell_boxes: bool = True,
    show_detections: bool = True,
    show_assignment: bool = True,
    length_quantile: float = 0.66,
    title: str | None = None,
):
    """Overlay the estimated grid on an image for visual QA.

    Layers: ``delaunay`` (raw triangulation), ``grid_edges`` (edges used for the angle, magenta),
    ``lattice`` (neighbour links: orange column steps, cyan row steps), ``cell_boxes`` (green solid
    where detected, red dashed where inferred), ``detections`` (white outlines) and ``assignment``
    (yellow line from each detection to its cell).
    """
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    if ax is None:
        _, ax = plt.subplots(figsize=(10, 10))
    if image is not None:
        ax.imshow(image)

    centers = grid.cell_centers
    w, h = grid.cell_size
    angle_deg = float(np.rad2deg(grid.angle_rad))

    # Delaunay and filtered grid edges.
    if (show_delaunay or show_grid_edges) and gdf is not None and len(gdf) >= 3:
        centroids, _ = _centroids_and_sizes(gdf)
        tri = Delaunay(centroids)
        edge_set: set[tuple[int, int]] = set()
        for s in tri.simplices:
            for a, b in ((s[0], s[1]), (s[1], s[2]), (s[0], s[2])):
                edge_set.add((min(a, b), max(a, b)))
        edges = np.array(sorted(edge_set))
        p1 = centroids[edges[:, 0]]
        p2 = centroids[edges[:, 1]]
        vec = p2 - p1
        lengths = np.linalg.norm(vec, axis=1)
        angles = np.arctan2(vec[:, 1], vec[:, 0])

        if show_delaunay:
            segs = np.stack([p1, p2], axis=1)
            from matplotlib.collections import LineCollection
            ax.add_collection(LineCollection(
                segs, colors="blue", linewidths=0.3, alpha=0.35,
            ))

        if show_grid_edges:
            cutoff = np.quantile(lengths, length_quantile)
            dev = _fold_to_quarter(angles - grid.angle_rad)
            keep = (np.abs(dev) < np.deg2rad(15.0)) & (lengths <= cutoff)
            if keep.any():
                segs = np.stack([p1[keep], p2[keep]], axis=1)
                from matplotlib.collections import LineCollection
                ax.add_collection(LineCollection(
                    segs, colors="magenta", linewidths=0.9, alpha=0.85,
                ))

    # Lattice links between adjacent cell centres.
    if show_lattice and grid.n_rows * grid.n_cols >= 2:
        from matplotlib.collections import LineCollection

        # Horizontal (column-step) connections: (i, j) -- (i, j+1)
        col_segs = []
        for i in range(grid.n_rows):
            for j in range(grid.n_cols - 1):
                col_segs.append([centers[i, j], centers[i, j + 1]])
        # Vertical (row-step) connections: (i, j) -- (i+1, j)
        row_segs = []
        for j in range(grid.n_cols):
            for i in range(grid.n_rows - 1):
                row_segs.append([centers[i, j], centers[i + 1, j]])
        if col_segs:
            ax.add_collection(LineCollection(
                col_segs, colors="orange", linewidths=0.8, alpha=0.85,
            ))
        if row_segs:
            ax.add_collection(LineCollection(
                row_segs, colors="deepskyblue", linewidths=0.8, alpha=0.85,
            ))
        # Lattice nodes
        flat = centers.reshape(-1, 2)
        ax.scatter(flat[:, 0], flat[:, 1], s=6, c="blue",
                   edgecolors="black", linewidths=0.3, zorder=4)

    # Cell boxes and indices.
    if show_cell_boxes:
        for i in range(grid.n_rows):
            for j in range(grid.n_cols):
                cx, cy = centers[i, j]
                missing = grid.assignment[i, j] == -1
                color = "red" if missing else "deepskyblue"
                ls = "--" if missing else "-"
                ax.add_patch(_rect_centered(
                    cx, cy, w, h, angle_deg,
                    edgecolor=color, facecolor="none",
                    linewidth=1.0, linestyle=ls,
                ))
                if show_indices:
                    ax.text(cx, cy, f"{i},{j}", color=color, fontsize=10,
                            ha="center", va="center", zorder=5)

    # Raw detections and assignment lines.
    if gdf is not None:
        if show_detections:
            for _, row in gdf.reset_index(drop=True).iterrows():
                minx, miny, maxx, maxy = row.geometry.bounds
                ax.add_patch(Rectangle(
                    (minx, miny), maxx - minx, maxy - miny,
                    edgecolor="blue", facecolor="none", linewidth=0.6,
                    alpha=0.7,
                ))
        if show_assignment:
            for i in range(grid.n_rows):
                for j in range(grid.n_cols):
                    det_idx = grid.assignment[i, j]
                    if det_idx < 0:
                        continue
                    bounds = gdf.geometry.iloc[det_idx].bounds
                    det_cx = 0.5 * (bounds[0] + bounds[2])
                    det_cy = 0.5 * (bounds[1] + bounds[3])
                    cell_cx, cell_cy = centers[i, j]
                    ax.plot([det_cx, cell_cx], [det_cy, cell_cy],
                            color="yellow", linewidth=2, alpha=0.6)

    if title is None:
        title = (
            f"grid {grid.n_rows}x{grid.n_cols}  "
            f"angle={np.rad2deg(grid.angle_rad):+.2f}deg  "
            f"occupancy={grid.occupancy:.0%}  "
            f"|col_vec|={np.linalg.norm(grid.col_vec):.1f}  "
            f"|row_vec|={np.linalg.norm(grid.row_vec):.1f}"
        )
    ax.set_title(title)
    return ax


def _rect_centered(cx, cy, w, h, angle_deg, **kwargs):
    """A matplotlib Rectangle centred on (cx, cy), rotated about its centre."""
    from matplotlib.patches import Rectangle

    return Rectangle(
        (cx - w / 2, cy - h / 2),
        w, h,
        angle=angle_deg,
        rotation_point="center",
        **kwargs,
    )


def _plot_polygon(ax, geom, **kwargs):
    """Draw a shapely polygon outline as a matplotlib patch."""
    from matplotlib.patches import Polygon as MplPolygon

    coords = np.asarray(geom.exterior.coords)
    kwargs.setdefault("facecolor", "none")
    kwargs.setdefault("fill", False)
    ax.add_patch(MplPolygon(coords, closed=True, **kwargs))


def plot_grid_gdf(
    cells,
    image=None,
    ax=None,
    show_ideal: bool = True,
    show_detections: bool = True,
    show_labels: bool = True,
    show_missing_labels: bool = False,
    show_indices: bool = False,
    class_colors: dict | None = None,
    title: str | None = None,
):
    """Plot the cells from ``TMAGrid.to_geodataframe()``, with each detection's class and conf.

    ``show_ideal`` draws the fit box at every cell, dashed red where inferred. ``class_colors`` is
    ``{class_name: color}``, auto-assigned if omitted.
    """
    import matplotlib.pyplot as plt

    if ax is None:
        _, ax = plt.subplots(figsize=(10, 10))
    if image is not None:
        ax.imshow(image)

    # One colour per class.
    classes = sorted({str(c) for c in cells["class"].tolist()})
    if class_colors is None:
        cycle = ["lime", "orange", "magenta", "yellow", "cyan", "deepskyblue"]
        class_colors = {c: cycle[i % len(cycle)] for i, c in enumerate(classes)}

    for _, r in cells.iterrows():
        ideal = r.get("ideal_geometry")
        real = r["geometry"]
        occupied = bool(r["occupied"])
        cls = str(r["class"])
        conf = float(r["conf"])
        color = class_colors.get(cls, "lime")

        if show_ideal and ideal is not None:
            _plot_polygon(
                ax, ideal,
                edgecolor="deepskyblue" if occupied else "red",
                linestyle="-" if occupied else "--",
                linewidth=1.0, alpha=0.8,
            )

        if show_detections and real is not None:
            _plot_polygon(ax, real, edgecolor=color, linewidth=1.6, zorder=4)

        # Detected cells show class and conf. Missing cells optionally show the class.
        label_this = (show_labels and occupied) or (show_missing_labels and not occupied)
        geom_for_label = real if real is not None else ideal
        if label_this and geom_for_label is not None:
            minx, miny, maxx, _ = geom_for_label.bounds
            cx = 0.5 * (minx + maxx)
            label = cls if conf < 0 else f"{cls} {conf:.2f}"
            ax.text(
                cx, miny, label,
                color="black", fontsize=8, ha="center", va="bottom",
                bbox={"boxstyle": "round,pad=0.15", "fc": color, "ec": "none", "alpha": 0.85},
                zorder=6,
            )

        if show_indices and ideal is not None:
            minx, miny, maxx, maxy = ideal.bounds
            ax.text(
                0.5 * (minx + maxx), 0.5 * (miny + maxy),
                f"{int(r['row'])},{int(r['col'])}",
                color="white", fontsize=7, ha="center", va="center", zorder=5,
            )

    if title is None:
        n_occ = int(cells["occupied"].sum())
        title = (
            f"{len(cells)} cells  "
            f"{n_occ} detected / {len(cells) - n_occ} inferred"
        )
    ax.set_title(title)
    return ax


def missing_cell_boxes(grid: TMAGrid):
    """Synthetic boxes for cells with no detection, as a GeoDataFrame.

    Tagged ``class="inferred_core"`` with NaN confidence.
    """
    import geopandas as gpd
    from shapely.geometry import box

    records = []
    centers = grid.cell_centers
    w, h = grid.cell_size
    for i in range(grid.n_rows):
        for j in range(grid.n_cols):
            if grid.assignment[i, j] != -1:
                continue
            cx, cy = centers[i, j]
            records.append(
                {
                    "row": i,
                    "col": j,
                    "class": "inferred_core",
                    "conf": float("nan"),
                    "geometry": box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                }
            )
    return gpd.GeoDataFrame(records, geometry="geometry", crs=None)