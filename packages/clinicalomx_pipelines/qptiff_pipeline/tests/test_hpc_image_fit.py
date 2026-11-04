"""Every deployment on the light image can run there.

For each deployment pinned to the light image, walks the intra-repo import graph, deferred imports
included, and checks nothing outside the image is reachable.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
PREFECT_YAML = pathlib.Path(__file__).resolve().parents[1] / "prefect.yaml"
PKG = "qptiff_pipeline"
LIGHT_IMAGE = "comx-qptiff"

#: What the light image provides: containers/Dockerfile.qptiff's install list, plus the transitive
#: dependencies this repo imports directly. When this test fails, add the package to the Dockerfile
#: and here, or leave the deployment on croi. Never add it here alone.
IN_LIGHT_IMAGE = frozenset({
    # installed explicitly by Dockerfile.qptiff
    "prefect", "lamindb", "clinicalomx", "bioio", "bioio_qptiff", "tifffile", "PIL", "numpy",
    # theirs, and imported directly somewhere in this repo
    "httpx",       # prefect's http client; orchestration.notify posts the Teams card with it
    "pandas",      # lamindb
    "pyarrow",     # lamindb
    "anndata",     # lamindb
    "yaml",        # prefect
    "pydantic",    # prefect, and a direct dependency of this repo
    "xarray", "dask", "zarr", "fsspec", "imagecodecs", "multiscale_spatial_image",  # bioio
})


def _module_path(dotted: str) -> pathlib.Path | None:
    p = SRC.joinpath(*dotted.split(".")).with_suffix(".py")
    return p if p.exists() else None


def _imports_of(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """(external top-level packages, intra-repo dotted modules) found ANYWHERE in the file."""
    external: set[str] = set()
    internal: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                (internal if top == PKG else external).add(alias.name if top == PKG else top)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # relative imports stay inside the package
            top = node.module.split(".")[0]
            if top == PKG:
                internal.add(node.module)
                # `from pkg.sub import name` may name a module. Paths that do not resolve are
                # skipped.
                internal.update(f"{node.module}.{a.name}" for a in node.names)
            else:
                external.add(top)
    return external, internal


def _reachable_externals(entrypoint_module: str) -> set[str]:
    seen: set[str] = set()
    external: set[str] = set()
    queue = [entrypoint_module]
    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        seen.add(dotted)
        path = _module_path(dotted)
        if path is None:
            continue
        ext, internal = _imports_of(path)
        external |= ext
        queue.extend(m for m in internal if m not in seen)
    return external - set(sys.stdlib_module_names)


def _light_deployments() -> list[tuple[str, str]]:
    """(deployment name, entrypoint module) for every deployment pinned to the light image.

    Parsed as text, not yaml: pyyaml is not a dependency.
    """
    out = []
    name = entry = image = None
    for line in PREFECT_YAML.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            if name and image and LIGHT_IMAGE in image:
                out.append((name, entry))
            name, entry, image = stripped.split(":", 1)[1].strip(), None, None
        elif stripped.startswith("entrypoint:") and entry is None:
            entry = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("image:") and image is None:
            image = stripped.split(":", 1)[1].strip()
    if name and image and LIGHT_IMAGE in image:
        out.append((name, entry))
    return out


LIGHT = _light_deployments()


def test_the_light_image_is_actually_in_use() -> None:
    assert LIGHT, f"no deployment pins {LIGHT_IMAGE}; this test would silently check nothing"


@pytest.mark.parametrize("name,entrypoint", LIGHT, ids=[n for n, _ in LIGHT])
def test_a_light_deployment_reaches_nothing_outside_its_image(name: str, entrypoint: str) -> None:
    module = entrypoint.split(":")[0].removeprefix("src/").removesuffix(".py").replace("/", ".")
    missing = sorted(_reachable_externals(module) - IN_LIGHT_IMAGE)
    assert not missing, (
        f"{name} runs on {LIGHT_IMAGE} but can reach {missing}, which that image does not carry. "
        f"Either add them to containers/Dockerfile.qptiff and to IN_LIGHT_IMAGE, or move this "
        f"deployment back to croi. This would otherwise fail as an ImportError inside a SLURM job."
    )


def test_the_walk_follows_deferred_and_indirect_imports() -> None:
    reachable = _reachable_externals(f"{PKG}.flows.data_tests")
    assert "geopandas" in reachable, (
        "the import walk no longer finds geopandas two hops away through checks/pipeline.py, so it "
        "can no longer prove anything about the light deployments"
    )
