"""``register_blocks.py`` imports a module the hpc-worker distribution publishes.

hpc-worker installs flat top-level modules, so the block class lives in ``connection``. The main
check parses the script instead of importing it, so it runs without prefect. The resolver tests
need real prefect. All skip outside the monorepo, where the clinicalomxdb tree is absent.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import tomllib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "register_blocks.py"
PACKAGES = pathlib.Path(__file__).resolve().parents[3]
HPC = PACKAGES / "clinicalomxdb" / "hub-pipelines" / "prefect" / "workers" / "hpc"
HPC_PYPROJECT = HPC / "pyproject.toml"

needs_sibling = pytest.mark.skipif(
    not HPC_PYPROJECT.is_file(),
    reason=f"hpc-worker source absent at {HPC} (standalone release checkout)",
)
needs_real_prefect = pytest.mark.skipif(
    getattr(sys.modules.get("prefect"), "__path__", None) == [],
    reason="conftest stubbed prefect; the real one carries prefect.blocks",
)


def constant(name: str) -> str:
    """The string a module-level ``name = "..."`` assignment binds, read without importing."""
    for node in ast.parse(SCRIPT.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{SCRIPT.name} no longer defines {name}")


def declared_py_modules() -> list[str]:
    return tomllib.loads(HPC_PYPROJECT.read_text())["tool"]["setuptools"]["py-modules"]


@pytest.fixture
def register_blocks(monkeypatch):
    """The script, loaded by path. It sits beside ``prefect.yaml``, not under ``src/``."""
    spec = importlib.util.spec_from_file_location("register_blocks_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hpc_connection(monkeypatch):
    """The real hpc-worker ``connection`` module, under its flat-install top-level name."""
    name = constant("HPC_BLOCK_MODULE")
    spec = importlib.util.spec_from_file_location(name, HPC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@needs_sibling
def test_declared_py_modules_publish_the_imported_name():
    imported = constant("HPC_BLOCK_MODULE")
    declared = declared_py_modules()
    assert imported in declared, (
        f"register_blocks.py imports `{imported}`, which {HPC_PYPROJECT} does not install. "
        f"It publishes {declared}. Either the import is wrong, or the worker was repackaged "
        f"and this consumer was not updated."
    )


@needs_sibling
def test_the_class_the_script_names_is_the_one_the_module_defines():
    wanted = constant("HPC_BLOCK_CLASS")
    source = (HPC / f"{constant('HPC_BLOCK_MODULE')}.py").read_text()
    defined = {
        node.name for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
    }
    assert wanted in defined, f"{wanted} is not defined in that module; it defines {defined}"


@needs_sibling
@needs_real_prefect
def test_resolver_returns_the_real_block_class(register_blocks, hpc_connection):
    resolved = register_blocks.container_registry_auth_block()
    assert resolved is getattr(hpc_connection, register_blocks.HPC_BLOCK_CLASS)
    # The Prefect server keys the saved document on this name.
    assert resolved._block_type_name == "Container Registry Auth"


@needs_real_prefect
def test_missing_install_names_the_interpreter(register_blocks, monkeypatch):
    monkeypatch.setattr(register_blocks, "HPC_BLOCK_MODULE", "hpc_worker")
    monkeypatch.delitem(sys.modules, "hpc_worker", raising=False)

    with pytest.raises(SystemExit) as caught:
        register_blocks.container_registry_auth_block()

    message = str(caught.value)
    assert register_blocks.SHARED_PREFECT_VENV in message, "must name the interpreter to use"
    assert register_blocks.HPC_SOURCE_PATH in message, "must name what to install"
    assert register_blocks.HPC_DISTRIBUTION in message


@needs_real_prefect
def test_the_import_is_resolved_before_any_token_is_read(register_blocks, monkeypatch):
    monkeypatch.setattr(register_blocks, "HPC_BLOCK_MODULE", "hpc_worker")
    monkeypatch.delitem(sys.modules, "hpc_worker", raising=False)
    for name in ("GHCR_USERNAME", "COMX_GHCR_READ", "COMX_CI_READ"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as caught:
        register_blocks.main()

    assert register_blocks.HPC_DISTRIBUTION in str(caught.value)
