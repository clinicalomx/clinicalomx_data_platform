"""Connectivity dearray: row,col from a walk over the detections' neighbour graph.

Every detection is a node, so none is dropped. Indices propagate locally, so a displaced block of
columns only affects the edges at its seam. A reviewer's link is a zero-cost edge in the graph.
Reuses ``tma_gridding``'s angle, derotation and pitch estimates. numpy and scipy only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ROW = "row"  # edge joins cells in the same row: moves `col`
COL = "col"  # edge joins cells in the same column: moves `row`

MAX_STEPS = 3       # reject an edge implying a jump of more than this many cells
SKEW_TOL = 0.45     # off-axis component allowed, as a fraction of the on-axis component
STEP_TOL = 0.28     # how far |length| may sit from k*pitch, as a fraction of pitch


@dataclass
class GraphGrid:
    """Assignment of detections to grid cells, plus a predicted position for every cell.

    rc:        per-detection ``[row, col]``, or None where nothing could be assigned.
    cell_pos:  ``{(row, col): (x, y)}`` for every site in range, predicted locally. Inferred cell
               boxes are drawn from these.
    cell_size: median detected box size, for synthesised boxes.
    """

    rc: list                      # list[list[int] | None], parallel to the input centroids
    cell_pos: dict                # {(row, col): (x, y)}
    cell_size: tuple
    n_rows: int
    n_cols: int
    angle_rad: float
    warnings: list = field(default_factory=list)

    @property
    def n_assigned(self) -> int:
        return sum(1 for v in self.rc if v is not None)


def _delaunay_edges(pts):
    """Unique undirected Delaunay edges as an (E, 2) index array (empty when degenerate)."""
    import numpy as np
    from scipy.spatial import Delaunay

    if len(pts) < 3:
        return np.zeros((0, 2), dtype=int)
    try:
        tri = Delaunay(pts)
    except Exception:
        return np.zeros((0, 2), dtype=int)
    seen = set()
    for s in tri.simplices:
        for a, b in ((s[0], s[1]), (s[1], s[2]), (s[0], s[2])):
            seen.add((min(int(a), int(b)), max(int(a), int(b))))
    return np.array(sorted(seen), dtype=int) if seen else np.zeros((0, 2), dtype=int)


def _step(v, dx, dy):
    """One derotated edge vector -> ``(axis, k, cost)``, or None if it is not a grid step.

    ``k`` is the signed number of cells the edge spans, so it can stride over missing cores.
    ``cost`` is near zero for an edge that looks like k clean steps.
    """
    vx, vy = float(v[0]), float(v[1])
    if abs(vx) >= abs(vy):
        axis, comp, pitch, off = ROW, vx, dx, abs(vy)
    else:
        axis, comp, pitch, off = COL, vy, dy, abs(vx)
    on = abs(comp)
    if not pitch or pitch <= 0 or on <= 1e-9:
        return None
    if off > SKEW_TOL * on:
        return None  # diagonal, not a grid step
    k = round(comp / pitch)
    if not 1 <= abs(k) <= MAX_STEPS:
        return None
    err = abs(on - abs(k) * pitch) / pitch
    if err > STEP_TOL:
        return None
    return axis, k, float(err + off / on)  # length error plus off-axis lean


def _classify(derot, edges, dx, dy):
    """Candidate edges -> accepted grid steps ``(i, j, axis, k, cost)``."""
    out = []
    for i, j in edges:
        s = _step(derot[j] - derot[i], dx, dy)
        if s is not None:
            out.append((int(i), int(j), s[0], s[1], s[2]))
    return out


def _link_edges(links, n, derot, dx, dy, warnings):
    """Reviewer links as zero-cost graph edges.

    A link skips the geometry tests. Its axis is trusted; only the step count and sign are measured.
    """
    out = []
    for lk in links or []:
        i, j = lk.get("a"), lk.get("b")
        if not (isinstance(i, int) and isinstance(j, int) and 0 <= i < n and 0 <= j < n):
            warnings.append("a link referenced a core that is not in this fit; ignored")
            continue
        if i == j:
            continue
        axis = COL if lk.get("axis") == COL else ROW
        v = derot[j] - derot[i]
        comp = float(v[0]) if axis == ROW else float(v[1])
        pitch = dx if axis == ROW else dy
        k = round(comp / pitch) if pitch and pitch > 0 else 0
        if k == 0:
            k = 1 if comp >= 0 else -1  # same cell by the pitch, but the reviewer says adjacent
        k = max(-MAX_STEPS, min(MAX_STEPS, k))
        out.append((int(i), int(j), axis, k, 0.0))
    return out


def _spanning_forest(n, steps):
    """Kruskal over the step edges. Returns (tree_edges, non_tree_edges, parent_of_component).

    Cheapest edges win, so links (cost 0) come first. The tree makes the walk deterministic.
    """
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tree, extra = [], []
    for e in sorted(steps, key=lambda s: s[4]):
        ra, rb = find(e[0]), find(e[1])
        if ra == rb:
            extra.append(e)
        else:
            parent[ra] = rb
            tree.append(e)
    return tree, extra, [find(i) for i in range(n)]


def _walk(n, tree):
    """Assign integer (row, col) per node by walking the tree from each component's first node."""
    adj: dict = {}
    for i, j, axis, k, _ in tree:
        adj.setdefault(i, []).append((j, axis, k))
        adj.setdefault(j, []).append((i, axis, -k))
    rc: list = [None] * n
    for seed in range(n):
        if rc[seed] is not None:
            continue
        rc[seed] = [0, 0]
        stack = [seed]
        while stack:
            cur = stack.pop()
            r, c = rc[cur]
            for nxt, axis, k in adj.get(cur, ()):
                if rc[nxt] is not None:
                    continue
                # a ROW edge keeps the row and steps the column; COL is the transpose
                rc[nxt] = [r, c + k] if axis == ROW else [r + k, c]
                stack.append(nxt)
    return rc


def _align_components(rc, comp, derot, dx, dy, warnings):
    """Put every component on one index frame, aligned to the largest component.

    Each component's origin is the mean of ``pos - (col*dx, row*dy)`` in the derotated frame. The
    index offset is the origin difference divided by the pitch. Always reported as a warning: a link
    across the gap removes the guess.
    """
    import numpy as np

    groups: dict = {}
    for i, g in enumerate(comp):
        if rc[i] is not None:
            groups.setdefault(g, []).append(i)
    if len(groups) < 2:
        return rc
    base = max(groups, key=lambda g: len(groups[g]))

    def origin(idxs):
        return np.mean(
            [derot[i] - np.array([rc[i][1] * dx, rc[i][0] * dy]) for i in idxs], axis=0
        )

    o_base = origin(groups[base])
    for g, idxs in groups.items():
        if g == base:
            continue
        d = origin(idxs) - o_base
        d_col = round(float(d[0]) / dx) if dx else 0
        d_row = round(float(d[1]) / dy) if dy else 0
        for i in idxs:
            rc[i] = [rc[i][0] + d_row, rc[i][1] + d_col]
    warnings.append(
        f"{len(groups)} disconnected group(s) of cores were aligned by position, not by "
        "connectivity. Link one core in each group to pin the alignment"
    )
    return rc


def _verify(rc, extra, warnings):
    """Report the non-tree edges the assignment contradicts, rather than obeying them."""
    bad = 0
    for i, j, axis, k, _ in extra:
        if rc[i] is None or rc[j] is None:
            continue
        if axis == ROW:
            ok = rc[i][0] == rc[j][0] and rc[j][1] - rc[i][1] == k
        else:
            ok = rc[i][1] == rc[j][1] and rc[j][0] - rc[i][0] == k
        if not ok:
            bad += 1
    if bad:
        warnings.append(
            f"{bad} neighbour relation(s) disagree with the chosen grid, usually a badly placed "
            "detection; check for a core boxed twice or a box that is not a core"
        )


def _cell_positions(rc, centroids, n_rows, n_cols, angle, dx, dy):
    """Predicted (x, y) for every site in range, in image px.

    Occupied sites use their detection's centroid. An empty site is extrapolated from its assigned
    neighbours. A site with no assigned neighbour uses the global lattice.
    """
    import numpy as np

    from qptiff_pipeline.review.tma_gridding import _rotation_matrix

    R = _rotation_matrix(angle)
    col_vec = R @ np.array([dx, 0.0])   # +1 column in image px
    row_vec = R @ np.array([0.0, dy])   # +1 row in image px

    pos: dict = {}
    for i, v in enumerate(rc):
        if v is not None:
            pos[(v[0], v[1])] = np.asarray(centroids[i], dtype=float)

    known = dict(pos)
    if not known:
        return {}
    origin = np.mean([p - r * row_vec - c * col_vec for (r, c), p in known.items()], axis=0)

    for r in range(n_rows):
        for c in range(n_cols):
            if (r, c) in pos:
                continue
            # nearest assigned sites in index space, extrapolated by the local step vectors
            est = [
                p + (r - kr) * row_vec + (c - kc) * col_vec
                for (kr, kc), p in known.items()
                if abs(kr - r) + abs(kc - c) <= 2
            ]
            pos[(r, c)] = (np.mean(est, axis=0) if est
                           else origin + r * row_vec + c * col_vec)
    return pos


def assign_by_connectivity(centroids, sizes, links=None, angle=None) -> GraphGrid:
    """Assign detections to grid cells by walking their neighbour graph.

    centroids: (N, 2) detection centres in image px.
    sizes:     (N, 2) detected box (w, h). Only the median is used.
    links:     reviewer links ``[{"a": i, "b": j, "axis": "row"|"col"}]``, where ``i`` and ``j``
               index ``centroids``. They enter as zero-cost edges.
    angle:     radians, to skip the estimate.
    """
    import numpy as np

    from qptiff_pipeline.review.tma_gridding import (
        derotate,
        estimate_axis_spacings,
        estimate_grid_angle,
    )

    warnings: list[str] = []
    centroids = np.asarray(centroids, dtype=float).reshape(-1, 2)
    n = len(centroids)
    if n == 0:
        return GraphGrid([], {}, (1.0, 1.0), 0, 0, 0.0, ["no detections to grid"])

    # The angle never comes from the links: links cross seams, so their direction is skewed.
    if angle is None:
        # Qhull raises on degenerate input, such as a single collinear row of cores.
        try:
            angle = float(estimate_grid_angle(centroids)) if n >= 3 else 0.0
        except Exception:
            angle = 0.0
            warnings.append("could not estimate a rotation angle; assuming the array is upright")

    derot = derotate(centroids, angle)
    try:
        dx, dy = estimate_axis_spacings(derot)
    except Exception:
        dx = dy = None
    if not dx or not dy:
        # Degenerate (a single row or column): use the span-based pitch.
        span_x = float(derot[:, 0].max() - derot[:, 0].min()) or 1.0
        span_y = float(derot[:, 1].max() - derot[:, 1].min()) or 1.0
        dx = dx or max(span_x / max(1, n - 1), 1.0)
        dy = dy or max(span_y / max(1, n - 1), 1.0)

    steps = _classify(derot, _delaunay_edges(derot), dx, dy)
    steps += _link_edges(links, n, derot, dx, dy, warnings)  # links address centroids by index

    tree, extra, comp = _spanning_forest(n, steps)
    rc = _walk(n, tree)
    rc = _align_components(rc, comp, derot, dx, dy, warnings)
    _verify(rc, extra, warnings)

    assigned = [v for v in rc if v is not None]
    if not assigned:
        return GraphGrid([None] * n, {}, (1.0, 1.0), 0, 0, angle,
                         warnings + ["no neighbour relations survived; cannot grid these cores"])

    # Normalise: row 0 topmost, col 0 leftmost.
    r0 = min(v[0] for v in assigned)
    c0 = min(v[1] for v in assigned)
    rc = [None if v is None else [v[0] - r0, v[1] - c0] for v in rc]
    assigned = [v for v in rc if v is not None]
    n_rows = max(v[0] for v in assigned) + 1
    n_cols = max(v[1] for v in assigned) + 1

    sizes = np.asarray(sizes, dtype=float).reshape(-1, 2) if len(sizes) else np.array([[dx, dy]])
    cell_size = tuple(float(v) for v in np.median(sizes, axis=0))

    return GraphGrid(
        rc=rc,
        cell_pos=_cell_positions(rc, centroids, n_rows, n_cols, angle, dx, dy),
        cell_size=cell_size,
        n_rows=int(n_rows),
        n_cols=int(n_cols),
        angle_rad=float(angle),
        warnings=warnings,
    )


def geometry_provider(g: GraphGrid) -> dict:
    """``GraphGrid`` -> the JSON-safe dict ``review.grid_links`` reads cell positions from.

    Carries explicit per-cell positions under ``cells``, keyed ``"row,col"``.
    """
    return {
        "cells": {f"{r},{c}": [float(p[0]), float(p[1])] for (r, c), p in g.cell_pos.items()},
        "cell_size": [float(v) for v in g.cell_size],
        "n_rows": int(g.n_rows),
        "n_cols": int(g.n_cols),
    }
