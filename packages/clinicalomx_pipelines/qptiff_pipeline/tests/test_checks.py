"""This repo's check module never writes to the instance."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

CHECKS_DIR = Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline" / "checks"

# Method names that only ever mutate, whatever the receiver.
LAMINDB_WRITES = {"save", "delete", "add_values", "remove_values", "set_values", "restore", "trash"}
# clinicalomx ROI writes, matched only on a `cx.` or `clinicalomx.` receiver: the bare names collide
# with stdlib methods.
FRAMEWORK_WRITES = {"annotate", "split", "merge", "create", "update"}
FRAMEWORK_RECEIVERS = {"cx", "clinicalomx"}


def _write_calls(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr, recv = node.func.attr, node.func.value
        if attr in LAMINDB_WRITES or (attr in FRAMEWORK_WRITES and isinstance(recv, ast.Name)
              and recv.id in FRAMEWORK_RECEIVERS):
            hits.append(node.lineno)
    return hits


def test_no_check_module_writes_to_the_instance():
    offenders = []
    for path in sorted(CHECKS_DIR.rglob("*.py")):
        lines = path.read_text().splitlines()
        offenders += [f"{path.name}:{n}  {lines[n - 1].strip()}"
                      for n in _write_calls(ast.parse(path.read_text()))]
    assert not offenders, "a check writes to the instance:\n" + "\n".join(offenders)


def test_the_write_guard_would_catch_a_mutation():
    assert _write_calls(ast.parse("record.features.add_values({'x': 1})"))
    assert _write_calls(ast.parse("artifact.delete(permanent=True)"))
    assert _write_calls(ast.parse("cx.annotate(roi, art, 'role')"))
    assert _write_calls(ast.parse("cx.split(art, kids, 'role', parent=p)"))
    # same method names, other receivers: not writes
    assert not _write_calls(ast.parse('key.split("/")[1]'))
    assert not _write_calls(ast.parse("counts.update({'a': 1})"))
    assert not _write_calls(ast.parse("gdf.attrs.get('scale_factor')"))


def test_the_checks_import_without_a_database():
    proc = subprocess.run(
        [sys.executable, "-c",
         ("import qptiff_pipeline.checks.pipeline, sys;"
          "print('yes' if 'lamindb' in sys.modules else '')")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "importing the checks pulled in lamindb (and so connected)"


def test_this_repo_declares_its_checks_to_the_framework():
    from clinicalomx.checks import Check

    from qptiff_pipeline.checks.pipeline import pipeline_checks

    checks = pipeline_checks()
    assert checks and all(isinstance(c, Check) for c in checks)
    assert all(c.name.startswith("pipeline.") for c in checks), "names must be owner-scoped"
