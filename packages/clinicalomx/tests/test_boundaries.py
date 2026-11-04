"""Package boundaries: the vocabulary constants import nothing heavy, the core imports no
orchestrator or model framework, and importing the package opens no database connection.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path


def _in_subprocess(code: str) -> str:
    """Run ``code`` in a clean interpreter and return stdout."""
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


#: Modules the vocabulary constants must not import.
_HEAVY = ("lamindb", "django", "psycopg2", "pydantic", "pydantic_settings", "bionty", "yaml")


def test_names_pulls_in_no_heavy_dependency():
    present = _in_subprocess(
        "import clinicalomx.names, sys;"
        f"print(','.join(m for m in {_HEAVY!r} if m in sys.modules))"
    )
    assert present == "", f"clinicalomx.names pulled in: {present}"


def test_core_import_is_orchestrator_and_model_framework_blind():
    present = _in_subprocess(
        "import clinicalomx, sys;"
        "print(','.join(m for m in ('prefect', 'mlflow') if m in sys.modules))"
    )
    assert present == "", f"importing clinicalomx pulled in: {present}"


def test_core_import_does_not_connect_to_a_database():
    present = _in_subprocess(
        "import clinicalomx, sys;"
        "print('yes' if 'lamindb' in sys.modules else '')"
    )
    assert present == "", "importing clinicalomx pulled in lamindb (and so connected)"


def test_lazy_exports_actually_resolve():
    import ast
    import pathlib

    import clinicalomx

    src = pathlib.Path(clinicalomx.__file__).parent
    defined: dict[str, set[str]] = {}

    def top_level(module: str) -> set[str]:
        if module in defined:
            return defined[module]
        names: set[str] = set()
        for node in ast.parse((src / f"{module}.py").read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        defined[module] = names
        return names

    wrong = [
        f"{name} -> {module}.py"
        for name, module in clinicalomx._EXPORTS.items()
        if name not in top_level(module)
    ]
    assert not wrong, f"_EXPORTS maps names to the wrong module: {wrong}"


def test_vocab_seeder_is_not_imported_by_the_core():
    present = _in_subprocess(
        "import clinicalomx, sys;"
        "print('yes' if 'clinicalomx.vocab' in sys.modules else '')"
    )
    assert present == "", "importing clinicalomx pulled in clinicalomx.vocab"


def test_no_subpackage_module_connects_when_imported():
    import pathlib

    import clinicalomx

    src = pathlib.Path(clinicalomx.__file__).parent
    modules = ["clinicalomx.snapshot"]
    for pkg in ("checks", "extract"):
        modules += [
            "clinicalomx." + ".".join(p.relative_to(src).with_suffix("").parts).removesuffix(
                ".__init__")
            for p in sorted((src / pkg).rglob("*.py"))
        ]
    modules = [m for m in modules if (src / pathlib.Path(*m.split(".")[1:])).with_suffix(
        ".py").exists() or (src / pathlib.Path(*m.split(".")[1:]) / "__init__.py").exists()]

    probe = (
        f"import sys; [__import__(m) for m in {modules!r}];"
        "banned = [m for m in ('lamindb', 'django', 'psycopg2') if m in sys.modules];"
        "print('PULLED:' + ','.join(banned)); sys.exit(1 if banned else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{proc.stdout.strip()} {proc.stderr.strip()[-400:]}"


def _extract_extra_modules() -> tuple[str, ...]:
    """The import names of the `extract` extra, from `pyproject.toml`, plus xarray and dask."""
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    names = [re.match(r"[A-Za-z0-9._-]+", r).group(0)
             for r in project["optional-dependencies"]["extract"]]
    return (*(n.replace("-", "_") for n in names), "xarray", "dask")


#: The `clinicalomx[extract]` stack. Planning modules must import none of it.
_EXTRACT_EXTRA = _extract_extra_modules()


def test_planning_does_not_import_the_builder_stack():
    probe = (
        "import sys;"
        "import clinicalomx.extract, clinicalomx.extract.recipes, clinicalomx.extract.docs,"
        " clinicalomx.extract.availability, clinicalomx.extract.plan,"
        " clinicalomx.extract.request, clinicalomx.extract.select,"
        " clinicalomx.extract.assemble, clinicalomx.extract.coords, clinicalomx.extract.tools,"
        # `layout` reaches the builders lazily, so it is on the planning side.
        " clinicalomx.extract.layout;"
        "from clinicalomx.extract import all_recipes; all_recipes();"
        "from clinicalomx.extract.docs import render; render();"
        f"pulled = [m for m in {_EXTRACT_EXTRA!r} if m in sys.modules];"
        "print('PULLED:' + ','.join(pulled)); sys.exit(1 if pulled else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{proc.stdout.strip()} {proc.stderr.strip()[-400:]}"


def test_the_builders_are_reachable_only_through_a_dotted_path():
    import importlib

    from clinicalomx.extract.recipes import FOUNDATION_RECIPES

    for recipe in FOUNDATION_RECIPES:
        # `placements`: one per ROI subtype on a mixed fan-out.
        paths = [("builder", recipe.builder),
                 *((f"placement[{t or 'default'}]", d) for t, d in recipe.placements.items())]
        for label, dotted in paths:
            module, _, attr = dotted.partition(":")
            obj = getattr(importlib.import_module(module), attr)
            assert callable(obj), f"{recipe.name} {label} {dotted} is not callable"


def test_resolving_every_lazy_export_still_does_not_connect():
    probe = (
        "import sys, clinicalomx as cx;"
        "[getattr(cx, n) for n in cx._EXPORTS];"
        "banned = [m for m in ('lamindb', 'django', 'psycopg2') if m in sys.modules];"
        "print('PULLED:' + ','.join(banned)); sys.exit(1 if banned else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{proc.stdout.strip()} {proc.stderr.strip()[-400:]}"
