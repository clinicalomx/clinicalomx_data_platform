"""review.grid_graph: connectivity dearray, on generated point sets.

The key case is an 11x10 array whose right-hand block sits half a pitch low.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from conftest import load_review_module

grid_graph = load_review_module("grid_graph")
assign = grid_graph.assign_by_connectivity

PITCH = 100.0
SIZE = 80.0
N_ROWS, N_COLS = 11, 10
SEAM = 6  # first column of the right-hand block


def _grid(n_rows=N_ROWS, n_cols=N_COLS, *, drop=(), shift_cols=(), shift=0.0, angle=0.0,
          gutter=0.0):
    """Synthetic core centroids plus the true (row, col) of each, in image px.

    drop:        cells to omit, as (row, col).
    shift_cols:  columns displaced in y by `shift` px.
    gutter:      extra x gap before `shift_cols`.
    angle:       whole-array rotation, radians.
    """
    pts, truth = [], []
    for r in range(n_rows):
        for c in range(n_cols):
            if (r, c) in drop:
                continue
            x = c * PITCH + (gutter if c in shift_cols else 0.0)
            y = r * PITCH + (shift if c in shift_cols else 0.0)
            if angle:
                ca, sa = math.cos(angle), math.sin(angle)
                x, y = x * ca - y * sa, x * sa + y * ca
            pts.append((x, y))
            truth.append((r, c))
    return np.array(pts), truth


def _sizes(n):
    return np.full((n, 2), SIZE)


def _as_map(g, truth):
    """{true (row, col) -> assigned (row, col)} for every detection that got an index."""
    return {truth[i]: tuple(v) for i, v in enumerate(g.rc) if v is not None}


def _is_consistent(g, truth):
    """The assignment matches the truth up to one global offset."""
    m = _as_map(g, truth)
    assert len(m) == len(truth), f"{len(truth) - len(m)} core(s) went unassigned"
    offs = {(a[0] - t[0], a[1] - t[1]) for t, a in m.items()}
    assert len(offs) == 1, f"assignment is not a rigid relabelling of the truth: offsets {offs}"
    return offs.pop()


# Clean cases.


def test_clean_grid_is_assigned_exactly():
    pts, truth = _grid()
    g = assign(pts, _sizes(len(pts)))
    assert _is_consistent(g, truth) == (0, 0)
    assert (g.n_rows, g.n_cols) == (N_ROWS, N_COLS)
    assert g.warnings == []
    assert g.n_assigned == len(pts)


def test_rotated_grid_gets_the_same_indices():
    pts, truth = _grid(angle=math.radians(7.0))
    g = assign(pts, _sizes(len(pts)))
    assert _is_consistent(g, truth) == (0, 0)
    assert (g.n_rows, g.n_cols) == (N_ROWS, N_COLS)


def test_missing_cores_do_not_shift_their_neighbours():
    holes = {(3, 3), (3, 4), (5, 0), (7, 8), (0, 9), (9, 5), (4, 4)}
    pts, truth = _grid(drop=holes)
    g = assign(pts, _sizes(len(pts)))
    assert _is_consistent(g, truth) == (0, 0)
    # every hole has a predicted position
    for h in holes:
        assert h in g.cell_pos


def test_a_long_run_of_holes_is_still_reachable():
    pts, truth = _grid(drop={(r, 4) for r in range(N_ROWS)})
    g = assign(pts, _sizes(len(pts)))
    m = _as_map(g, truth)
    assert len(m) == len(truth)
    # columns either side of the gap keep distinct indices
    assert {m[(0, 3)][1], m[(0, 5)][1]} == {m[(0, 3)][1], m[(0, 3)][1] + 2}


# The block-offset case.


def test_offset_block_assigns_every_core():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    g = assign(pts, _sizes(len(pts)))

    assert g.n_assigned == len(pts), "no core may be left unassigned"
    m = _as_map(g, truth)
    # within each block the relabelling is rigid
    for block in (range(SEAM), range(SEAM, N_COLS)):
        offs = {(m[t][0] - t[0], m[t][1] - t[1]) for t in truth if t[1] in block}
        assert len(offs) == 1, f"block {block} is not internally consistent: {offs}"


def test_offset_block_beats_the_lattice_fit():
    tma_gridding = load_review_module("tma_gridding")
    pts, _truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    sizes = _sizes(len(pts))

    lat = tma_gridding._fit_once(pts, sizes, None, None, 0.5)
    spacing = 0.5 * (np.linalg.norm(lat.row_vec) + np.linalg.norm(lat.col_vec))
    inl = np.isfinite(lat.residuals) & (lat.residuals <= 0.4 * spacing)
    if 4 <= inl.sum() < len(pts):  # the refine pass in tma_gridding
        lat = tma_gridding._fit_once(pts[inl], sizes[inl], None, None, 0.5)
        a, r = tma_gridding._assign_detections(pts, lat.cell_centers, None, 0.5 * spacing)
        lat.assignment, lat.residuals = a, r
    lattice_placed = int((lat.assignment >= 0).sum())

    graph = assign(pts, sizes)

    assert lattice_placed < len(pts), (
        "fixture no longer reproduces the lattice fit's core loss. If tma_gridding improved, "
        "revisit whether this module is still needed"
    )
    assert lat.n_rows > N_ROWS, "expected the lattice fit to invent extra rows"
    assert graph.n_assigned == len(pts)
    assert (graph.n_rows, graph.n_cols) == (N_ROWS, N_COLS)


def test_offset_block_keeps_row_zero_of_the_right_block():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    g = assign(pts, _sizes(len(pts)))
    m = _as_map(g, truth)
    first_row = min(r for r, _ in m.values())
    for c in range(N_COLS):
        rows = [r for (r, cc) in m.values() if cc == m[(0, c)][1]]
        assert min(rows) == first_row, f"column of true col {c} does not start on the first row"


def test_offset_block_inferred_cell_lands_on_its_own_block():
    shift = 0.5 * PITCH
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=shift, drop={(5, 7)})
    g = assign(pts, _sizes(len(pts)))
    m = _as_map(g, truth)

    # derive (5,7)'s index from its column neighbour (4,7)
    r_of = {t: m[t] for t in m}
    target = (r_of[(4, 7)][0] + 1, r_of[(4, 7)][1])
    assert target in g.cell_pos
    y = float(g.cell_pos[target][1])
    assert y == pytest.approx(5 * PITCH + shift, abs=0.25 * PITCH), (
        "inferred cell should sit on the shifted block, not on the unshifted lattice"
    )


# Links as constraints.


def test_link_pins_a_disconnected_block():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH, gutter=1.6 * PITCH)

    g = assign(pts, _sizes(len(pts)))
    assert any("disconnected" in w for w in g.warnings), g.warnings

    i = truth.index((0, SEAM - 1))
    j = truth.index((0, SEAM))
    g2 = assign(pts, _sizes(len(pts)), links=[{"a": i, "b": j, "axis": "row"}])
    assert not any("disconnected" in w for w in g2.warnings), g2.warnings
    m = _as_map(g2, truth)
    assert m[(0, SEAM - 1)][0] == m[(0, SEAM)][0], "the linked cores must share a row"
    assert g2.n_assigned == len(pts)


def test_link_overrides_the_geometry_it_contradicts():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    i = truth.index((2, SEAM - 1))
    j = truth.index((2, SEAM))
    g = assign(pts, _sizes(len(pts)), links=[{"a": i, "b": j, "axis": "row"}])
    m = _as_map(g, truth)
    assert m[(2, SEAM - 1)][0] == m[(2, SEAM)][0]
    assert m[(2, SEAM)][1] - m[(2, SEAM - 1)][1] == 1


def test_link_to_a_missing_index_is_reported_not_fatal():
    pts, truth = _grid()
    g = assign(pts, _sizes(len(pts)), links=[{"a": 0, "b": 9999, "axis": "row"}])
    assert any("not in this fit" in w for w in g.warnings)
    assert _is_consistent(g, truth) == (0, 0)


def test_a_seam_link_must_not_set_the_rotation_angle():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    i, j = truth.index((0, SEAM - 1)), truth.index((0, SEAM))

    tilt = math.atan2(*(pts[j] - pts[i])[::-1])
    assert abs(math.degrees(tilt)) > 20, "fixture should present a badly tilted seam link"

    g = assign(pts, _sizes(len(pts)), links=[{"a": i, "b": j, "axis": "row"}])
    assert abs(math.degrees(g.angle_rad)) < 5, (
        f"angle {math.degrees(g.angle_rad):.1f}deg was taken from the tilted link"
    )
    assert g.n_assigned == len(pts)
    m = _as_map(g, truth)
    assert m[(0, SEAM - 1)][0] == m[(0, SEAM)][0], "the link must still hold"


def test_adding_a_link_never_degrades_a_good_fit():
    pts, truth = _grid(shift_cols=range(SEAM, N_COLS), shift=0.5 * PITCH)
    before = assign(pts, _sizes(len(pts)))
    i, j = truth.index((0, SEAM - 1)), truth.index((0, SEAM))
    after = assign(pts, _sizes(len(pts)), links=[{"a": i, "b": j, "axis": "row"}])

    assert after.n_assigned == before.n_assigned == len(pts)
    assert (after.n_rows, after.n_cols) == (before.n_rows, before.n_cols)
    assert _as_map(after, truth) == _as_map(before, truth)


# Degenerate input.


def test_no_detections():
    g = assign(np.zeros((0, 2)), np.zeros((0, 2)))
    assert g.rc == [] and g.n_rows == 0
    assert any("no detections" in w for w in g.warnings)


def test_single_row_still_indexes():
    pts, truth = _grid(n_rows=1, n_cols=6)
    g = assign(pts, _sizes(len(pts)))
    m = _as_map(g, truth)
    assert len(m) == 6
    assert len({r for r, _ in m.values()}) == 1, "a single row must not be split across rows"
    assert sorted(c for _, c in m.values()) == list(range(6)), "columns must stay distinct"
    assert (g.n_rows, g.n_cols) == (1, 6)


def test_geometry_provider_is_json_safe():
    import json

    pts, _truth = _grid(n_rows=4, n_cols=4, drop={(1, 1)})
    g = assign(pts, _sizes(len(pts)))
    p = grid_graph.geometry_provider(g)
    assert json.loads(json.dumps(p)) == p
    assert "1,1" in p["cells"]  # the hole carries a predicted position
    assert p["n_rows"] == 4 and p["n_cols"] == 4
