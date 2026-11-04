"""Source artifacts are selected by key, never by the ``Artifact.suffix`` field.

lamindb stores ``.qptiff`` as ``""`` in that field on newer rows, so a suffix filter skips them.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline"


def _suffix_lookups(tree: ast.AST) -> list[int]:
    """Line numbers of ``.filter(...)`` or ``.exclude(...)`` calls passing ``suffix=``, by AST."""
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("filter", "exclude")
                and any(kw.arg == "suffix" for kw in node.keywords)):
            hits.append(node.lineno)
    return hits


def _offending_lines() -> list[str]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        lines = text.splitlines()
        for n in _suffix_lookups(ast.parse(text)):
            out.append(f"{path.relative_to(SRC)}:{n}  {lines[n - 1].strip()}")
    return out


def test_no_flow_queries_the_suffix_field():
    offenders = _offending_lines()
    assert not offenders, (
        "queried Artifact.suffix; use queries.source_artifacts (key__endswith) instead:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_the_source_query_matches_on_the_key():
    text = (SRC / "lamin" / "queries.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(text))
              if isinstance(n, ast.FunctionDef) and n.name == "source_artifacts")
    calls = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "filter"]
    assert calls, "source_artifacts no longer queries at all"
    kwargs = {kw.arg for c in calls for kw in c.keywords}
    assert "key__endswith" in kwargs, f"source_artifacts stopped matching on the key: {kwargs}"
    assert "suffix" not in kwargs, "source_artifacts reintroduced a suffix field lookup"


def test_the_guard_would_catch_the_original_bug():
    assert _suffix_lookups(ast.parse("ln.Artifact.filter(suffix=suffix, is_latest=True)"))
    assert _suffix_lookups(ast.parse('ln.Artifact.filter(suffix=".qptiff", is_latest=True)'))
    assert _suffix_lookups(ast.parse('matching.exclude(suffix="")'))
    # and not on the key-based form
    assert not _suffix_lookups(ast.parse("ln.Artifact.filter(key__endswith=s, is_latest=True)"))
    assert not _suffix_lookups(ast.parse('x = "do not use filter(suffix=...)"'))
