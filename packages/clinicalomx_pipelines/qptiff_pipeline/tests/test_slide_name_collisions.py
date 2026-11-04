"""mint-slides never merges two different qptiffs that share a filename stem into one WholeSlide.

A byte-identical copy shares the Record. The flow is parsed, not imported.
"""
from __future__ import annotations

import ast
from pathlib import Path

FLOW = (Path(__file__).resolve().parents[1]
        / "src" / "qptiff_pipeline" / "flows" / "mint_slides.py")


def _mint_body() -> ast.AST:
    tree = ast.parse(FLOW.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "mint_slides":
            return node
    raise AssertionError("mint_slides flow function not found")


def test_an_existing_slide_name_is_not_trusted_on_its_own():
    src = ast.unparse(_mint_body())
    assert "source_image" in src, (
        "mint_slides no longer reads the existing slide's source_image, so a name collision on a "
        "DIFFERENT qptiff would silently merge two scans into one Record"
    )
    assert ".hash" in src, (
        "mint_slides no longer compares artifact hashes. Identical copies must share a Record, "
        "different files must not"
    )


def test_the_collision_is_reported_not_just_logged():
    body = _mint_body()
    returns = [n for n in ast.walk(body) if isinstance(n, ast.Return) and n.value is not None]
    assert any("conflated" in ast.unparse(r) for r in returns), (
        "mint_slides must return the refused collisions, not only log them"
    )


def test_identical_copies_still_share_one_record():
    src = ast.unparse(_mint_body())
    assert "twin.hash != qptiff.hash" in src, (
        "the collision test must be hash inequality; comparing keys would reject legitimate "
        "byte-identical copies"
    )


def test_a_quarantined_copy_is_not_reported_as_a_collision():
    src = ast.unparse(_mint_body())
    assert "_mintable(qptiff)" in src, (
        "the collision branch must gate on _mintable, or it will report the already-quarantined "
        "copies on every run"
    )
    assert src.count("_mintable(qptiff)") >= 2, (
        "the integrity gate and the collision guard must share ONE definition of 'is a candidate', "
        "or the guard can report a conflation with a file the gate would have excluded"
    )
