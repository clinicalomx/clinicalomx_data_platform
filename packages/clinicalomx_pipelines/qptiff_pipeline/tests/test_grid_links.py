"""review.grid_links: reviewer links that repair a dearray's indexing.

The fixture is an 11x10 TMA whose right-hand block (cols 6-9) was assigned one row too far down.
One link across the seam should fix it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Load the module from disk, outside the package.
_SRC = Path(__file__).resolve().parents[1] / "src/qptiff_pipeline/review/grid_links.py"
_spec = importlib.util.spec_from_file_location("grid_links", _SRC)
grid_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid_links)

apply_grid_links = grid_links.apply_grid_links

PITCH = 100.0          # lattice spacing, px, both axes
SIZE = 80.0            # core box size
N_ROWS, N_COLS = 11, 10
SEAM = 6               # first column of the shifted block
SHIFT_PX = 45.0        # how far the right block physically sits below the lattice

GRID = {
    "origin": [0.0, 0.0],
    "row_vec": [0.0, PITCH],
    "col_vec": [PITCH, 0.0],
    "cell_size": [SIZE, SIZE],
    "n_rows": N_ROWS,
    "n_cols": N_COLS,
}


def _box(bid, row, col, *, occupied=True, extra_y=0.0):
    """A canvas box on cell (row, col), optionally displaced in y."""
    cx, cy = col * PITCH, row * PITCH + extra_y
    return {
        "class": "full_core" if occupied else "inferred_core",
        "xyxy": [cx - SIZE / 2, cy - SIZE / 2, cx + SIZE / 2, cy + SIZE / 2],
        "occupied": occupied,
        "rc": [row, col],
        "bid": bid,
    }


@pytest.fixture
def offset_block():
    """The mis-indexed grid, as the dearray hands it to the canvas.

    Left block (cols 0-5): correct, rows 0-10. Right block (cols 6-9): cores assigned to rows 1-10,
    row 0 inferred, boxes SHIFT_PX below their cell centres. Row 10 of the right block has no core.
    """
    boxes, bid = [], 0
    for row in range(N_ROWS):
        for col in range(N_COLS):
            if col < SEAM:
                boxes.append(_box(bid, row, col))
            elif row == 0:
                boxes.append(_box(bid, row, col, occupied=False))
            elif row == N_ROWS - 1:
                # the block's last core; after the shift it lands on row 9, leaving row 10 empty
                boxes.append(_box(bid, row, col, extra_y=SHIFT_PX - PITCH))
            else:
                boxes.append(_box(bid, row, col, extra_y=SHIFT_PX - PITCH))
            bid += 1
    return boxes


def _at(boxes, row, col):
    hits = [b for b in boxes if b.get("rc") == [row, col]]
    assert len(hits) == 1, f"expected exactly one box at {row},{col}, got {len(hits)}"
    return hits[0]


def _seam_link(boxes):
    """'0,5 and 1,6 are the same row': the one link that fixes the block."""
    return [{"a": _at(boxes, 0, SEAM - 1)["bid"], "b": _at(boxes, 1, SEAM)["bid"], "axis": "row"}]


def test_seam_link_reindexes_the_whole_block(offset_block):
    out, warnings = apply_grid_links(offset_block, _seam_link(offset_block), GRID)

    assert warnings == []
    # the block moved up one row: 1,6 is now 0,6 and a real detection
    assert _at(out, 0, SEAM)["occupied"] is True
    for col in range(SEAM, N_COLS):
        assert _at(out, 0, col)["occupied"] is True, f"0,{col} should now hold a real core"
    # the block's last row is now empty
    for col in range(SEAM, N_COLS):
        assert _at(out, N_ROWS - 1, col)["occupied"] is False
    # the grid did not change size
    assert max(b["rc"][0] for b in out) + 1 == N_ROWS
    assert max(b["rc"][1] for b in out) + 1 == N_COLS


def test_left_block_is_untouched(offset_block):
    before = {b["bid"]: (list(b["rc"]), list(b["xyxy"])) for b in offset_block if b["rc"][1] < SEAM}
    out, _ = apply_grid_links(offset_block, _seam_link(offset_block), GRID)
    after = {b["bid"]: (list(b["rc"]), list(b["xyxy"])) for b in out if b.get("bid") in before}
    assert after == before


def test_detected_geometry_is_never_moved(offset_block):
    before = {b["bid"]: list(b["xyxy"]) for b in offset_block if b["occupied"]}
    out, _ = apply_grid_links(offset_block, _seam_link(offset_block), GRID)
    for b in out:
        if b.get("occupied") and b.get("bid") in before:
            assert b["xyxy"] == before[b["bid"]]


def test_inferred_cells_in_a_shifted_block_carry_its_offset(offset_block):
    out, _ = apply_grid_links(offset_block, _seam_link(offset_block), GRID)

    for col in range(SEAM, N_COLS):
        cell = _at(out, N_ROWS - 1, col)
        assert cell["occupied"] is False
        cy = 0.5 * (cell["xyxy"][1] + cell["xyxy"][3])
        assert cy == pytest.approx((N_ROWS - 1) * PITCH + SHIFT_PX, abs=1.0)


def test_unshifted_block_inferred_cells_stay_on_the_lattice(offset_block):
    boxes = [b for b in offset_block if not (b["rc"][0] == 5 and b["rc"][1] == 2)]
    boxes.append({**_box(999, 5, 2, occupied=False)})
    out, _ = apply_grid_links(boxes, _seam_link(boxes), GRID)
    cell = _at(out, 5, 2)
    assert cell["occupied"] is False
    cx = 0.5 * (cell["xyxy"][0] + cell["xyxy"][2])
    cy = 0.5 * (cell["xyxy"][1] + cell["xyxy"][3])
    assert (cx, cy) == pytest.approx((2 * PITCH, 5 * PITCH), abs=1.0)


def test_link_that_already_holds_is_a_no_op(offset_block):
    link = [{"a": _at(offset_block, 3, 0)["bid"], "b": _at(offset_block, 3, 1)["bid"], "axis": "row"}]
    out, warnings = apply_grid_links(offset_block, link, GRID)
    assert warnings == []
    for b in offset_block:
        assert _at(out, *b["rc"])["occupied"] == b["occupied"]


def test_no_links_returns_input_unchanged(offset_block):
    out, warnings = apply_grid_links(offset_block, [], GRID)
    assert out == offset_block
    assert warnings == []


def test_missing_grid_warns_instead_of_guessing(offset_block):
    out, warnings = apply_grid_links(offset_block, _seam_link(offset_block), None)
    assert out == offset_block
    assert any("dearray first" in w for w in warnings)


def test_a_link_inside_the_block_does_not_fragment_it(offset_block):
    links = _seam_link(offset_block) + [
        {"a": _at(offset_block, 1, SEAM)["bid"], "b": _at(offset_block, 2, SEAM + 1)["bid"],
         "axis": "row"},
    ]
    out, warnings = apply_grid_links(offset_block, links, GRID)

    # every column of the block moved by one, together
    for col in range(SEAM, N_COLS):
        assert _at(out, 0, col)["occupied"] is True, f"0,{col} should hold a real core"
        assert _at(out, N_ROWS - 1, col)["occupied"] is False
    # the link that could not be honoured is reported
    assert any("disagrees with the seam" in w for w in warnings), warnings


def test_extra_links_across_the_same_seam_confirm_rather_than_compound(offset_block):
    links = _seam_link(offset_block) + [
        {"a": _at(offset_block, 2, SEAM - 1)["bid"], "b": _at(offset_block, 3, SEAM)["bid"],
         "axis": "row"},
        {"a": _at(offset_block, 4, SEAM - 1)["bid"], "b": _at(offset_block, 5, SEAM)["bid"],
         "axis": "row"},
    ]
    out, warnings = apply_grid_links(offset_block, links, GRID)
    assert warnings == [], warnings
    assert _at(out, 0, SEAM)["occupied"] is True
    assert max(b["rc"][0] for b in out) + 1 == N_ROWS  # no double shift


def test_conflicting_links_are_reported(offset_block):
    links = _seam_link(offset_block) + [
        # contradicts the first link
        {"a": _at(offset_block, 2, SEAM - 1)["bid"], "b": _at(offset_block, 1, SEAM)["bid"],
         "axis": "row"},
    ]
    out, warnings = apply_grid_links(offset_block, links, GRID)
    assert len(warnings) == 1, f"one bad link should give exactly one message: {warnings}"
    assert "disagrees with the seam at 5|6" in warnings[0]
    assert _at(out, 0, SEAM)["occupied"] is True  # the first link still took effect
    assert max(b["rc"][0] for b in out) + 1 == N_ROWS  # and the block moved once, not twice


def test_link_to_a_deleted_box_is_ignored(offset_block):
    links = [{"a": _at(offset_block, 0, 0)["bid"], "b": 10_000, "axis": "row"}]
    out, warnings = apply_grid_links(offset_block, links, GRID)
    assert any("no longer exists" in w for w in warnings)
    assert len(out) == len(offset_block)


def test_ungridded_core_claims_the_cell_the_shift_frees(offset_block):
    boxes = [b for b in offset_block if b["rc"] != [2, SEAM]]  # the fit dropped this one
    stray = _box(500, 2, SEAM, extra_y=SHIFT_PX - PITCH)
    stray["rc"] = None  # so it has no cell
    boxes.append(stray)

    out, warnings = apply_grid_links(boxes, _seam_link(boxes), GRID)

    assert warnings == []
    claimed = _at(out, 1, SEAM)          # 2,SEAM shifted up one row
    assert claimed["bid"] == 500, "the stray core should have taken the freed cell"
    assert claimed["occupied"] is True
    assert not [b for b in out if b.get("rc") is None], "nothing should be left un-gridded"


def test_a_genuinely_stray_box_is_not_forced_onto_the_lattice(offset_block):
    stray = _box(501, 0, 0)
    stray["rc"] = None
    stray["xyxy"] = [5000.0, 5000.0, 5080.0, 5080.0]  # far off the array
    boxes = offset_block + [stray]

    out, _ = apply_grid_links(boxes, _seam_link(boxes), GRID)
    leftover = [b for b in out if b.get("rc") is None]
    assert [b["bid"] for b in leftover] == [501]


def _cells_provider():
    """The connectivity fit's geometry shape: explicit per-cell positions, no global lattice."""
    return {
        "cells": {f"{r},{c}": [c * PITCH, r * PITCH] for r in range(N_ROWS) for c in range(N_COLS)},
        "cell_size": [SIZE, SIZE],
        "n_rows": N_ROWS,
        "n_cols": N_COLS,
    }


def test_seam_shift_works_against_an_explicit_cell_map(offset_block):
    out, warnings = apply_grid_links(offset_block, _seam_link(offset_block), _cells_provider())
    assert warnings == []
    for col in range(SEAM, N_COLS):
        assert _at(out, 0, col)["occupied"] is True
        assert _at(out, N_ROWS - 1, col)["occupied"] is False
    assert max(b["rc"][0] for b in out) + 1 == N_ROWS


def test_cell_map_outside_its_range_is_extrapolated(offset_block):
    provider = _cells_provider()
    # drop the last row from the map, so it must be extrapolated
    provider["cells"] = {k: v for k, v in provider["cells"].items()
                         if not k.startswith(f"{N_ROWS - 1},")}
    out, _ = apply_grid_links(offset_block, _seam_link(offset_block), provider)
    cell = _at(out, N_ROWS - 1, N_COLS - 1)
    cy = 0.5 * (cell["xyxy"][1] + cell["xyxy"][3])
    assert cy == pytest.approx((N_ROWS - 1) * PITCH, abs=0.6 * PITCH)


class _FakeGrid:
    """Stand-in for tma_gridding.TMAGrid."""
    origin = [1.5, 2.5]
    row_vec = [0.0, 100.0]
    col_vec = [100.0, 0.0]
    cell_size = [80.0, 80.0]
    n_rows = 11
    n_cols = 10


def test_grid_params_is_json_safe():
    import json

    p = grid_links.grid_params(_FakeGrid())
    assert json.loads(json.dumps(p)) == p
    assert p["origin"] == [1.5, 2.5]
    assert p["n_rows"] == 11 and p["n_cols"] == 10


def test_phantom_row_is_dropped(offset_block):
    out, _ = apply_grid_links(offset_block, _seam_link(offset_block), GRID)
    rows_with_cores = {b["rc"][0] for b in out if b.get("occupied")}
    assert rows_with_cores == set(range(N_ROWS))  # every row still earns its place
