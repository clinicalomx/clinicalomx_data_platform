"""Grid links: reviewer-declared adjacency that repairs a dearray's cell indexing.

A link says "these two cells are in the same row" (or column). Applying links only re-indexes;
nothing is refit:

    link (0,5) -- (1,6), axis="row"   =>   delta = 0 - 1 = -1
    seam sits between col 5 and col 6  =>  every column >= 6 shifts up one row

Box dicts are the canvas's own shape:
    {"class": str, "xyxy": [x1, y1, x2, y2], "occupied": bool, "rc": [row, col] | None, "bid": int}
Links reference the stable ``bid``, never a list index. numpy only.
"""
from __future__ import annotations

ROW = "row"  # link asserts: same row, adjacent columns
COL = "col"  # link asserts: same column, adjacent rows


def grid_params(grid) -> dict:
    """The lattice of a ``tma_gridding.TMAGrid`` as plain, JSON-safe lists."""
    return {
        "origin": [float(v) for v in grid.origin],
        "row_vec": [float(v) for v in grid.row_vec],
        "col_vec": [float(v) for v in grid.col_vec],
        "cell_size": [float(v) for v in grid.cell_size],
        "n_rows": int(grid.n_rows),
        "n_cols": int(grid.n_cols),
    }


def _cell_centre(grid: dict, row: int, col: int):
    """(x, y) of cell (row, col), from either geometry shape.

    - ``cells``: an explicit ``{"row,col": [x, y]}`` map, from ``grid_graph.geometry_provider``.
    - ``origin`` / ``row_vec`` / ``col_vec``: the global lattice from :func:`grid_params`.

    The map wins where it has the cell. Otherwise the lattice is used, else the nearest known cell
    is extrapolated.
    """
    import numpy as np

    cells = grid.get("cells")
    if cells:
        hit = cells.get(f"{row},{col}")
        if hit is not None:
            return np.asarray(hit, dtype=float)
    if "origin" in grid:
        return (
            np.asarray(grid["origin"], dtype=float)
            + row * np.asarray(grid["row_vec"], dtype=float)
            + col * np.asarray(grid["col_vec"], dtype=float)
        )
    if cells:
        # No lattice and the cell is outside the map: step out from the nearest known cell.
        keys = [tuple(int(v) for v in k.split(",")) for k in cells]
        pts = {k: np.asarray(cells[f"{k[0]},{k[1]}"], dtype=float) for k in keys}
        rows = sorted({k[0] for k in keys})
        cols = sorted({k[1] for k in keys})
        rv = _median_step(pts, keys, axis=0) if len(rows) > 1 else np.array([0.0, 1.0])
        cv = _median_step(pts, keys, axis=1) if len(cols) > 1 else np.array([1.0, 0.0])
        near = min(keys, key=lambda k: abs(k[0] - row) + abs(k[1] - col))
        return pts[near] + (row - near[0]) * rv + (col - near[1]) * cv
    raise KeyError("grid carries neither an explicit cell map nor a lattice")


def _median_step(pts: dict, keys: list, *, axis: int):
    """Median position change for a +1 step along ``axis`` (0 = row, 1 = col)."""
    import numpy as np

    deltas = []
    for k in keys:
        nxt = (k[0] + 1, k[1]) if axis == 0 else (k[0], k[1] + 1)
        if nxt in pts:
            deltas.append(pts[nxt] - pts[k])
    return np.median(np.vstack(deltas), axis=0) if deltas else np.array([0.0, 0.0])


def _pitch(grid: dict) -> float:
    """Mean cell-to-cell spacing, from either geometry shape (see :func:`_cell_centre`)."""
    import numpy as np

    if "row_vec" in grid:
        return 0.5 * (
            float(np.linalg.norm(np.asarray(grid["row_vec"], dtype=float)))
            + float(np.linalg.norm(np.asarray(grid["col_vec"], dtype=float)))
        )
    cells = grid.get("cells") or {}
    if cells:
        keys = [tuple(int(v) for v in k.split(",")) for k in cells]
        pts = {k: np.asarray(cells[f"{k[0]},{k[1]}"], dtype=float) for k in keys}
        rv = _median_step(pts, keys, axis=0)
        cv = _median_step(pts, keys, axis=1)
        norms = [float(np.linalg.norm(v)) for v in (rv, cv) if float(np.linalg.norm(v)) > 1e-9]
        if norms:
            return sum(norms) / len(norms)
    # Last resort: the cell size, which is close to the pitch.
    return float(np.mean(np.asarray(grid.get("cell_size", [1.0, 1.0]), dtype=float)))


def _box_centre(b: dict):
    import numpy as np

    x1, y1, x2, y2 = (float(v) for v in b["xyxy"])
    return np.array([0.5 * (x1 + x2), 0.5 * (y1 + y2)])


def _gridded(boxes: list[dict]) -> list[dict]:
    """Boxes that carry a grid position (``rc``)."""
    return [b for b in boxes if b.get("rc") is not None]


# The grid's own rc-adjacency edges are drawn client-side in roi_canvas/frontend/main.js.


def _apply_seams(
    gridded: list[dict], edges: list[dict], axis: str
) -> tuple[dict[int, int], list[str], set[tuple]]:
    """Resolve one axis' links into a per-lane index offset.

    Returns ``(offset_by_lane, warnings, reported)``. ``reported`` holds the ``(a, b)`` bid pairs
    already warned about. For ``axis=ROW`` the shift is a row shift, applied per column.

    Exactly one seam: the boundary nearest the origin that any link crosses. Everything beyond it
    moves together. Other links are checked against the result and reported, never applied.
    """
    idx, lane = (0, 1) if axis == ROW else (1, 0)  # (index we shift, lane we shift it by)
    by_bid = {b["bid"]: b for b in gridded if b.get("bid") is not None}
    warnings: list[str] = []
    reported: set[tuple] = set()
    what = "columns" if axis == ROW else "rows"

    pairs = []
    for e in edges:
        if e.get("axis") != axis:
            continue
        a, b = by_bid.get(e.get("a")), by_bid.get(e.get("b"))
        if a is None or b is None:
            warnings.append("a link referenced a box that no longer exists; ignored")
            reported.add((e.get("a"), e.get("b")))
            continue
        if int(a["rc"][idx]) == int(b["rc"][idx]):
            continue  # already consistent
        pairs.append((a, b))
    if not pairs:
        return {}, warnings, reported

    # The seam is the boundary nearest the origin that any correcting link crosses.
    def boundary(ab):
        return min(int(ab[0]["rc"][lane]), int(ab[1]["rc"][lane]))

    pairs.sort(key=boundary)
    a, b = pairs[0]
    la, lb = int(a["rc"][lane]), int(b["rc"][lane])
    if la == lb:
        warnings.append(
            f"link joins two cells in {what[:-1]} {la}, so it names no seam; ignored"
        )
        reported.add((a.get("bid"), b.get("bid")))
        return {}, warnings, reported
    delta = int(a["rc"][idx]) - int(b["rc"][idx])
    if abs(la - lb) != 1:
        warnings.append(
            f"link spans {what} {la} and {lb}, which are not adjacent; using {min(la, lb)}|"
            f"{min(la, lb) + 1} as the seam"
        )
    lanes = {int(x["rc"][lane]) for x in gridded}
    far = [x for x in lanes if (x >= lb if lb > la else x <= lb)]
    offset = {x: delta for x in far}

    # Remaining links are checks. Report the ones the seam cannot satisfy.
    for a2, b2 in pairs[1:]:
        pa = int(a2["rc"][idx]) + offset.get(int(a2["rc"][lane]), 0)
        pb = int(b2["rc"][idx]) + offset.get(int(b2["rc"][lane]), 0)
        if pa != pb:
            warnings.append(
                f"link {a2['rc'][0]},{a2['rc'][1]} to {b2['rc'][0]},{b2['rc'][1]} disagrees with the "
                f"seam at {min(la, lb)}|{min(la, lb) + 1} (it wants a {pa - pb:+d} shift of "
                f"{what[:-1]} {int(b2['rc'][lane])}); left as-is"
            )
            reported.add((a2.get("bid"), b2.get("bid")))
    return offset, warnings, reported


def apply_grid_links(
    boxes: list[dict], edges: list[dict], grid: dict | None
) -> tuple[list[dict], list[str]]:
    """Re-index the grid so every link holds, and rebuild the inferred cells.

    Returns ``(new_boxes, warnings)``, row-major. Detected boxes keep their geometry; only ``rc``
    changes. Row links are resolved before column links.
    """
    import numpy as np

    if not edges:
        return list(boxes), []
    if grid is None:
        return list(boxes), ["no grid on record: dearray first, then apply links"]

    gridded = _gridded(boxes)
    loose = [b for b in boxes if b.get("rc") is None]  # boxes drawn after the dearray
    if not gridded:
        return list(boxes), ["no gridded boxes to re-index; dearray first"]

    row_off, warnings, reported = _apply_seams(gridded, edges, ROW)
    col_off, w2, rep2 = _apply_seams(gridded, edges, COL)
    warnings += w2
    reported |= rep2

    # Shift the detections only. Inferred cells are regenerated below and do not affect the bounds,
    # so a row no detection lands on disappears.
    placed = []
    for b in gridded:
        if not b.get("occupied", True):
            continue
        r, c = int(b["rc"][0]), int(b["rc"][1])
        placed.append((r + row_off.get(c, 0), c + col_off.get(r, 0), b))
    if not placed:
        return list(boxes), ["no detected cores to re-index (every cell is inferred)"]
    r0 = min(p[0] for p in placed)
    c0 = min(p[1] for p in placed)
    placed = [(r - r0, c - c0, b) for r, c, b in placed]
    n_rows = max(p[0] for p in placed) + 1
    n_cols = max(p[1] for p in placed) + 1

    # Two detections on one cell are reported, not dropped. The second keeps its box.
    occupied: dict[tuple[int, int], dict] = {}
    clashes: list[dict] = []
    for r, c, b in placed:
        if (r, c) in occupied:
            clashes.append({**b, "rc": [r, c]})
            warnings.append(f"two detections both landed on cell {r},{c} after re-indexing")
            continue
        occupied[(r, c)] = {**b, "rc": [r, c]}

    # Inferred cells of a shifted block carry the block's offset from the lattice, so the crop
    # lands on the tissue.
    block_of = {c: row_off.get(c, 0) for c in range(n_cols)}
    deltas: dict[int, np.ndarray] = {}
    for shift in set(block_of.values()):
        resid = [
            _box_centre(b) - _cell_centre(grid, r, c)
            for (r, c), b in occupied.items()
            if block_of.get(c, 0) == shift
        ]
        deltas[shift] = np.median(np.vstack(resid), axis=0) if resid else np.zeros(2)

    w, h = (float(v) for v in grid["cell_size"])
    next_bid = max([b["bid"] for b in boxes if b.get("bid") is not None], default=-1) + 1

    # A detection the fit never placed (rc=None) takes the nearest free cell within one pitch.
    # Otherwise it stays un-gridded, which blocks Accept.
    pitch = _pitch(grid)
    still_loose: list[dict] = []
    for b in loose:
        if not b.get("occupied", True):
            still_loose.append(b)
            continue
        centre = _box_centre(b)
        best, best_d = None, pitch
        for r in range(n_rows):
            for c in range(n_cols):
                if (r, c) in occupied:
                    continue
                d = float(np.linalg.norm(centre - (_cell_centre(grid, r, c)
                                                   + deltas.get(block_of.get(c, 0), np.zeros(2)))))
                if d < best_d:
                    best, best_d = (r, c), d
        if best is None:
            still_loose.append(b)
        else:
            occupied[best] = {**b, "rc": [best[0], best[1]]}
    loose = still_loose

    out: list[dict] = []
    for r in range(n_rows):
        for c in range(n_cols):
            hit = occupied.get((r, c))
            if hit is not None:
                out.append(hit)
                continue
            cx, cy = _cell_centre(grid, r, c) + deltas.get(block_of.get(c, 0), np.zeros(2))
            out.append({
                "class": "inferred_core",
                "xyxy": [round(float(cx - w / 2), 1), round(float(cy - h / 2), 1),
                         round(float(cx + w / 2), 1), round(float(cy + h / 2), 1)],
                "occupied": False,
                "rc": [r, c],
                "bid": next_bid,
            })
            next_bid += 1
    out += clashes + loose

    # Check every link holds in the output. Links already reported are skipped.
    pos = {b["bid"]: b["rc"] for b in out if b.get("bid") is not None and b.get("rc")}
    for e in edges:
        if (e.get("a"), e.get("b")) in reported:
            continue
        ra, rb = pos.get(e.get("a")), pos.get(e.get("b"))
        if ra is None or rb is None:
            continue
        i = 0 if e.get("axis") == ROW else 1
        if ra[i] != rb[i]:
            warnings.append(
                f"link {ra[0]},{ra[1]} to {rb[0]},{rb[1]} could not be satisfied "
                f"(conflicts with another link)"
            )
    return out, warnings
