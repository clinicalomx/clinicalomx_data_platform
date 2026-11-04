"""A stage's run cites its environment, and does so after ``ln.track``. Checked on the AST."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TRACKING = SRC / "qptiff_pipeline" / "lamin" / "tracking.py"


def _call_lines(tree: ast.AST, name: str) -> list[int]:
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(name)]


def test_the_stage_run_cites_its_environment_after_it_exists():
    tree = ast.parse(TRACKING.read_text())
    track = _call_lines(tree, "ln.track")
    cite = _call_lines(tree, "link_apptainer_to_run")
    assert len(track) == 1, "one ln.track per stage run"
    assert len(cite) == 1, "track_prefect_run must cite the environment exactly once"
    assert cite[0] > track[0], "cite after ln.track, or ln.context.run is not the stage's run yet"


def test_the_environment_citation_comes_from_the_framework():
    tree = ast.parse(TRACKING.read_text())
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and any(a.name == "link_apptainer_to_run" for a in n.names)}
    assert sources == {"clinicalomx.environments"}
