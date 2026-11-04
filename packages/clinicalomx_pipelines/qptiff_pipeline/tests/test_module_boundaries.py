"""Every cross-module reference resolves to a name that exists in the target module.

Catches a name imported from the wrong module, or ``states.X`` after ``X`` moved: errors lint does
not see. Flow modules are parsed, not imported.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
TESTS = pathlib.Path(__file__).resolve().parent

#: Modules importable with no database and no orchestrator, as ``module name -> subpackage``.
#: domain/ imports with only pydantic, lamin/ needs an instance, orchestration/ drives it.
PURE_MODULES = {
    "states": "domain", "vocab": "domain", "keys": "domain", "contexts": "domain",
    "hitl": "domain", "roi_io": "domain", "governance": "domain",
    "registry": "lamin", "linking": "lamin", "queries": "lamin", "tracking": "lamin",
    "context_io": "lamin",
    "automations": "orchestration", "events": "orchestration", "gate": "orchestration",
    "notify": "orchestration",
}


@pytest.fixture(scope="module")
def modules():
    return {m: importlib.import_module(f"qptiff_pipeline.{pkg}.{m}")
            for m, pkg in PURE_MODULES.items()}


def _references(path: pathlib.Path):
    """``(lineno, module, name, kind)`` for every attribute read this file makes into our modules.

    Reads of attributes the file assigns first (fakes) are skipped.
    """
    tree = ast.parse(path.read_text())
    alias: dict[str, str] = {}
    assigned: set[tuple[str, str]] = set()
    out = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in {f"qptiff_pipeline.{p}"
                               for p in ("domain", "lamin", "orchestration")}:
                for a in node.names:
                    if a.name in PURE_MODULES:
                        alias[a.asname or a.name] = a.name
            elif node.module.startswith("qptiff_pipeline."):
                sub = node.module.rsplit(".", 1)[-1]
                if sub in PURE_MODULES:
                    out += [(node.lineno, sub, a.name, "import") for a in node.names]
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    assigned.add((t.value.id, t.attr))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id in alias
                and (node.value.id, node.attr) not in assigned):
            out.append((node.lineno, alias[node.value.id], node.attr, "attribute"))
    return out


def _sources():
    return sorted(SRC.rglob("*.py")) + sorted(TESTS.rglob("*.py"))


def test_every_cross_module_reference_resolves(modules):
    broken = []
    for path in _sources():
        for lineno, mod, name, kind in _references(path):
            if not hasattr(modules[mod], name):
                rel = path.relative_to(path.parents[len(path.parents) - 6])
                broken.append(f"{rel}:{lineno}  {kind}: {mod}.{name}")
    assert not broken, "references that would raise at run time:\n" + "\n".join(broken)


def _top_level_names(path: pathlib.Path) -> set[str]:
    """Names a module binds at module scope, by parsing it. ``import *`` is not resolved."""
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.Try):  # conditional imports at module scope
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names |= {(a.asname or a.name).split(".")[0] for a in sub.names}
    return names


def test_intra_repo_imports_name_something_that_exists():
    broken = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("qptiff_pipeline.")):
                continue
            target = SRC / pathlib.Path(*node.module.split(".")).with_suffix(".py")
            if not target.exists() or target == path:
                continue
            exported = _top_level_names(target)
            if "*" in exported:
                continue
            for a in node.names:
                if a.name != "*" and a.name not in exported:
                    broken.append(f"{path.name}:{node.lineno}  {node.module} has no {a.name!r}")
    assert not broken, "imports that would raise ImportError:\n" + "\n".join(broken)


def test_the_checker_finds_a_planted_break(tmp_path, modules):
    f = tmp_path / "planted.py"
    f.write_text(
        "from qptiff_pipeline.domain import states\n"
        "from qptiff_pipeline.domain.states import NOT_A_REAL_NAME\n"
        "x = states.ALSO_NOT_REAL\n"
    )
    found = {(name, kind) for _l, _m, name, kind in _references(f)
             if not hasattr(modules["states"], name)}
    assert found == {("NOT_A_REAL_NAME", "import"), ("ALSO_NOT_REAL", "attribute")}


def test_a_faked_module_attribute_is_not_reported(tmp_path, modules):
    f = tmp_path / "fake.py"
    f.write_text(
        "from qptiff_pipeline.domain import states\n"
        "states.INVENTED = 'x'\n"
        "y = states.INVENTED\n"
    )
    assert not [1 for _l, m, name, _k in _references(f) if not hasattr(modules[m], name)]


def test_the_domain_tier_imports_with_only_pydantic():
    mods = ", ".join(f"qptiff_pipeline.domain.{m}"
                     for m, pkg in PURE_MODULES.items() if pkg == "domain")
    heavy = ("lamindb", "prefect", "geopandas", "pandas", "streamlit", "numpy", "django")
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import {mods}, sys; print(','.join(m for m in {heavy!r} if m in sys.modules))"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"domain/ pulled in: {proc.stdout.strip()}"


def test_domain_never_imports_from_lamin_or_orchestration():
    offenders = []
    for path in sorted((SRC / "qptiff_pipeline" / "domain").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                for tier in ("lamin", "orchestration"):
                    if node.module.startswith(f"qptiff_pipeline.{tier}"):
                        offenders.append(f"{path.name}:{node.lineno} imports {node.module}")
    assert not offenders, "domain/ must not depend on a higher tier:\n" + "\n".join(offenders)
