"""The installed clinicalomx defines what this package imports from it.

Reads the installed modules without importing the app, through ``find_spec`` and the AST, so it
needs no UI framework or database, and never skips. Checks that each symbol exists and accepts the
keyword the app passes. It follows the published wheel, not the source tree.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

#: The Explorer entry points a published wheel may carry, newest first, as ``(module, symbol)``.
ENTRY_POINTS = (
    ("clinicalomx.extract.app.web", "make_app"),  # the Reflex app factory
    ("clinicalomx.extract.app.main", "render"),  # the Streamlit body
)
KEYWORD = "live"

#: Every other symbol the app imports from the installed wheel, as ``(module, name)``.
ALSO_IMPORTED = (("clinicalomx.extract.submit", "use_prefect"),)

#: The first wheel whose Explorer lifts Django's async guard. Checked only once the ``build``
#: extra's
#: floor reaches it.
ASYNC_GUARD_FLOOR = Version("0.3.5")
#: The environment variable that lifts the guard. Django reads it at call time.
ASYNC_GUARD_ENV = "DJANGO_ALLOW_ASYNC_UNSAFE"
#: The seam that sets it, as ``(module under web/, function)``.
ASYNC_GUARD_SEAM = ("cache", "allow_blocking_reads")
#: The package the Explorer's modules import each other from.
WEB_PACKAGE = "clinicalomx.extract.app.web"

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def _installed_entry_point() -> ast.FunctionDef:
    """The installed Explorer entry point, parsed with ``find_spec`` and the AST, not executed."""
    tried = []
    for module, symbol in ENTRY_POINTS:
        tried.append(module)
        spec = _spec(module)
        if spec is None or not spec.origin:
            continue
        tree = ast.parse(pathlib.Path(spec.origin).read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                return node
    raise AssertionError(
        f"the installed clinicalomx carries none of {tried}, so it has no Explorer this app "
        f"could host. This package declares clinicalomx{_declared_floor()}: publish the wheel "
        f"(tag clinicalomx-v<version>) and re-resolve before building any image.")


def _spec(module: str):
    """``find_spec``, but ``None`` when a parent package is absent, instead of raising."""
    try:
        return importlib.util.find_spec(module)
    except ModuleNotFoundError:
        return None


def _declared_floor() -> str:
    """The floor the ``build`` extra declares for clinicalomx."""
    reqs = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["build"]
    for raw in reqs:
        req = Requirement(raw)
        if req.name == "clinicalomx":
            return str(req.specifier) or "(no floor declared)"
    return "(the build extra no longer names clinicalomx)"


def test_the_installed_clinicalomx_defines_an_explorer_entry_point() -> None:
    assert _installed_entry_point().name in {symbol for _, symbol in ENTRY_POINTS}


def test_the_newest_entry_point_wins_where_the_wheel_has_both() -> None:
    if _spec(ENTRY_POINTS[0][0]) is not None:
        assert _installed_entry_point().name == ENTRY_POINTS[0][1]


def test_an_absent_entry_point_is_skipped_rather_than_raised() -> None:
    assert _spec("clinicalomx.extract.app.no_such_module") is None
    assert _spec("no_such_distribution.at.all") is None


def test_the_entry_point_takes_the_live_keyword_the_app_passes() -> None:
    fn = _installed_entry_point()
    accepted = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert KEYWORD in accepted or fn.args.kwarg is not None, (
        f"{fn.name}() accepts {sorted(accepted)}; the app passes {KEYWORD}=")


def test_the_installed_clinicalomx_defines_what_else_the_app_imports() -> None:
    for module, symbol in ALSO_IMPORTED:
        spec = _spec(module)
        assert spec and spec.origin, (
            f"the installed clinicalomx has no {module}, which the Build app imports. This "
            f"package declares clinicalomx{_declared_floor()} for it: publish that wheel "
            f"(tag clinicalomx-v<version>) and re-resolve before building any image.")
        tree = ast.parse(pathlib.Path(spec.origin).read_text())
        defined = {n.name for n in tree.body
                   if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}
        assert symbol in defined, (
            f"{spec.origin} defines no top-level {symbol}(), which the Build app imports. "
            f"Declared floor: clinicalomx{_declared_floor()}.")


def test_the_build_extra_states_a_floor_for_the_explorer() -> None:
    assert ">=" in _declared_floor(), (
        "the build extra names clinicalomx with no floor; a range that merely admits the wheel "
        "the Build app needs lets Dockerfile.build resolve an older one")


def _floored_at(version: Version) -> bool:
    """Whether the ``build`` extra's floor for clinicalomx excludes everything below ``version``."""
    reqs = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["build"]
    for raw in reqs:
        req = Requirement(raw)
        if req.name == "clinicalomx":
            return not req.specifier.contains(str(_previous(version)))
    return False


def _previous(version: Version) -> Version:
    """The largest release strictly under ``version``.

    Decrements the rightmost non-zero segment, so ``0.4.0`` gives a release below it.
    """
    release = list(version.release)
    last = max(i for i, segment in enumerate(release) if segment)
    return Version(".".join(str(s) for s in [*release[:last], release[last] - 1, 99999999]))


def test_djangos_async_guard_is_lifted_by_the_name_this_package_expects() -> None:
    import asyncio
    import os

    from django.core.exceptions import SynchronousOnlyOperation
    from django.utils.asyncio import async_unsafe

    @async_unsafe
    def read():
        return "rows"

    async def from_a_handler():
        return read()

    was = os.environ.pop(ASYNC_GUARD_ENV, None)
    try:
        with pytest.raises(SynchronousOnlyOperation):
            asyncio.run(from_a_handler())
        os.environ[ASYNC_GUARD_ENV] = "1"
        assert asyncio.run(from_a_handler()) == "rows"
    finally:
        os.environ.pop(ASYNC_GUARD_ENV, None)
        if was is not None:
            os.environ[ASYNC_GUARD_ENV] = was


def test_the_installed_explorer_can_read_the_instance_at_all() -> None:
    if not _floored_at(ASYNC_GUARD_FLOOR):
        pytest.skip(f"the build extra still admits clinicalomx below {ASYNC_GUARD_FLOOR}")
    module, function = ASYNC_GUARD_SEAM
    assert _lifts_the_guard(module, function), (
        f"the installed {module}.{function}() leaves {ASYNC_GUARD_ENV} unset, so every instance "
        "read this app makes raises SynchronousOnlyOperation on Reflex's event loop and the page "
        f"reports 0 ROIs against a healthy instance. Declared floor: clinicalomx"
        f"{_declared_floor()}.")
    assert _make_app_calls(module, function), (
        f"the installed make_app() never calls {module}.{function}(), which is the only way that "
        f"variable reaches the process: the function can lift the guard and the deployed page "
        f"still reads nothing. Declared floor: clinicalomx{_declared_floor()}.")


def _web_package() -> pathlib.Path:
    """The installed Explorer's directory, located without importing it."""
    spec = _spec(WEB_PACKAGE)
    assert spec and spec.origin, (
        f"the installed clinicalomx has no {WEB_PACKAGE}, so its Explorer predates the floor "
        f"this package declares: clinicalomx{_declared_floor()}.")
    return pathlib.Path(spec.origin).parent


def _lifts_the_guard(module: str, function: str) -> bool:
    """Whether the installed ``<module>.<function>()`` sets the guard, by calling it.

    The module is loaded from its file under a throwaway name. It imports only the standard library.
    """
    loading = importlib.util.spec_from_file_location(
        f"_installed_clinicalomx_{module}_under_test", _web_package() / f"{module}.py")
    assert loading and loading.loader, f"the installed Explorer has no {module}.py"
    loaded = importlib.util.module_from_spec(loading)
    loading.loader.exec_module(loaded)

    was = os.environ.pop(ASYNC_GUARD_ENV, None)
    try:
        getattr(loaded, function)()
        return bool(os.environ.get(ASYNC_GUARD_ENV))
    finally:
        os.environ.pop(ASYNC_GUARD_ENV, None)
        if was is not None:
            os.environ[ASYNC_GUARD_ENV] = was


def _make_app_calls(module: str, function: str) -> bool:
    """Whether the installed ``make_app`` calls ``<module>.<function>``."""
    tree = ast.parse((_web_package() / "__init__.py").read_text())
    bound = {alias.asname or alias.name
             for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and node.module == WEB_PACKAGE
             for alias in node.names if alias.name == module}
    entry = next((n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == ENTRY_POINTS[0][1]), None)
    assert entry is not None, (
        f"the installed Explorer defines no {ENTRY_POINTS[0][1]}(), so it predates the floor "
        f"this package declares: clinicalomx{_declared_floor()}.")
    return any(isinstance(call.func, ast.Attribute) and call.func.attr == function
               and isinstance(call.func.value, ast.Name) and call.func.value.id in bound
               for call in ast.walk(entry) if isinstance(call, ast.Call))
