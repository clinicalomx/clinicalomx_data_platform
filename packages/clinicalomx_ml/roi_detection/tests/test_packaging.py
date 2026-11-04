"""Packaging claims: one wheel root, `roi_detection/src/croi`, with each objective's configs
shipped as package data."""
from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys
import tomllib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
WHEEL = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]

#: Vocabulary owners persisted in the live instance. The entry-point key may change; the owner may not.
OWNERS = {
    "croi-general": "croi.objectives.tissue_objects.vocab:VOCAB",
    "croi-core": "croi.objectives.tma_core.vocab:VOCAB",
}

OBJECTIVES = ("tissue_objects", "tma_core")
SRC = "roi_detection/src"
OBJECTIVES_DIR = f"{SRC}/croi/objectives"


def test_one_distribution_named_clinical_roi():
    assert PYPROJECT["project"]["name"] == "clinical-roi"
    assert "workspace" not in PYPROJECT.get("tool", {}).get("uv", {})


def test_both_vocabulary_rows_are_in_one_entry_point_table():
    assert PYPROJECT["project"]["entry-points"]["clinicalomx.vocabulary"] == OWNERS


class _Vocabulary:
    """Stand-in for `clinicalomx.vocab.Vocabulary`, which this suite does not install. Only `owner`."""

    def __init__(self, owner: str):
        self.owner = owner
        self.features: dict[str, object] = {}
        self.types: dict[str, object] = {}

    def feature(self, name: str, dtype: object, description: str = "") -> str:
        self.features[name] = dtype
        return name

    def type(self, name: str, description: str = "", *, parent: str | None = None) -> str:
        self.types[name] = parent
        return name


class _StubLamindb(types.ModuleType):
    """Stub lamindb: vocabularies use `ln.Artifact` and friends only as dtype sentinels."""

    def __getattr__(self, name: str) -> type:
        cls = type(name, (), {})
        setattr(self, name, cls)
        return cls


@pytest.fixture
def objective_imports_are_isolated():
    """Import the real objective modules and remove them afterwards."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.split(".")[0] == "croi":
            del sys.modules[name]


@pytest.mark.parametrize("owner", sorted(OWNERS))
def test_the_owner_string_is_declared_by_the_vocabulary_itself(
    monkeypatch, objective_imports_are_isolated, owner: str
):
    """`registry.discover()` keys on `vocab.owner`, so it must match the entry-point name.

    A Feature cannot be deleted once used.
    """
    target, _, attr = OWNERS[owner].partition(":")

    monkeypatch.setitem(sys.modules, "lamindb", _StubLamindb("lamindb"))
    clinicalomx = types.ModuleType("clinicalomx")
    vocab_module = types.ModuleType("clinicalomx.vocab")
    vocab_module.Vocabulary = _Vocabulary
    clinicalomx.vocab = vocab_module
    monkeypatch.setitem(sys.modules, "clinicalomx", clinicalomx)
    monkeypatch.setitem(sys.modules, "clinicalomx.vocab", vocab_module)

    monkeypatch.syspath_prepend(str(ROOT / SRC))
    importlib.invalidate_caches()
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)

    vocab = getattr(importlib.import_module(target), attr)
    assert vocab.owner == owner
    assert vocab.features, f"{target} declared no features"


def test_no_two_wheel_package_roots_share_a_destination():
    """Two `packages` entries sharing a destination break the editable install. `dev-mode-dirs` is refused."""
    destinations = [pathlib.PurePath(p).name for p in WHEEL["packages"]]
    assert sorted(destinations) == sorted(set(destinations)), (
        f"two wheel roots share a destination: {destinations}. An editable install will lose one; "
        "give each destination one root rather than re-adding dev-mode-dirs."
    )
    assert "dev-mode-dirs" not in WHEEL


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_each_objective_is_one_flat_directory_with_its_record(obj: str):
    root = ROOT / OBJECTIVES_DIR / obj
    assert (root / "__init__.py").is_file()
    assert (root / "objective.py").is_file()
    assert sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != "__pycache__") == [
        "configs", "flows",
    ]


#: A directory slide images were once committed into by accident.
OLD_WORKING_DATA = "roi_detection/objectives"

#: Image suffixes, case-folded.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".qptiff", ".svs")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git; a failure fails the test."""
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=check)


def test_no_image_is_tracked_in_this_package():
    """No image is tracked anywhere in the package index."""
    tracked = _git("ls-files", "-z", "--", ".").stdout.split("\0")
    images = sorted(p for p in tracked if p.lower().endswith(IMAGE_SUFFIXES))
    assert images == [], f"images are tracked; their home is a lamindb Collection: {images[:10]}"


def test_the_old_working_data_directory_stays_empty_and_ignored():
    """Nothing is tracked under it, and the ignore rules cover it."""
    assert _git("ls-files", "--", OLD_WORKING_DATA).stdout == ""

    probe = f"{OLD_WORKING_DATA}/general/data/images/slide.png"
    assert _git("check-ignore", "-q", "--no-index", probe, check=False).returncode == 0, (
        f"{probe} is not ignored; restore `{OLD_WORKING_DATA}/` in the package .gitignore"
    )


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_configs_are_package_data_under_a_wheel_root(obj: str):
    """The stage yamls ship inside the wheel: under a `packages` entry and not excluded.

    tissue_objects has seven stage yamls plus the manifest; tma_core five plus the manifest.
    """
    floor = {"tissue_objects": 8, "tma_core": 6}[obj]
    configs = sorted(p.name for p in (ROOT / OBJECTIVES_DIR / obj / "configs").glob("*.yaml"))
    assert "manifest.yaml" in configs and len(configs) >= floor, configs
    assert "exclude" not in WHEEL and "only-include" not in WHEEL


def test_croi_is_the_one_wheel_root():
    assert WHEEL["packages"] == [f"{SRC}/croi"]
    assert (ROOT / SRC / "croi" / "__init__.py").is_file()


def _dockerignore_matcher():
    """`.dockerignore` as a predicate over context-relative paths, using Docker's matching rules.

    ``*`` and ``?`` never cross ``/``, ``**`` spans segments, and a directory match covers its contents.
    """
    lines = [
        line.strip()
        for line in (ROOT / "roi_detection/containers/.dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    def compile_one(pattern: str) -> re.Pattern[str]:
        out, i = [], 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out.append("(?:[^/]*/)*")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif pattern[i] == "*":
                out.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        return re.compile("^" + "".join(out) + "(?:/.*)?$")

    rules = [(pattern, compile_one(pattern)) for pattern in lines]

    def ignored(path: str) -> str | None:
        """The first pattern that excludes `path`, or None."""
        return next((pattern for pattern, rx in rules if rx.match(path)), None)

    return ignored


@pytest.mark.parametrize("module", (
    "croi/objectives/tma_core/dataset.py",
    "croi/objectives/tissue_objects/dataset.py",
    "croi/utils/roi_records.py",
))
def test_the_docker_context_does_not_ignore_a_data_module(module: str):
    """No pattern may drop a data module from the image's wheel."""
    ignored = _dockerignore_matcher()

    path = f"clinicalomx_ml/{SRC}/{module}"
    assert ignored(path) is None, f"{ignored(path)!r} drops {module} out of the wheel"


def test_the_docker_context_still_excludes_the_heavy_directories():
    """The matcher still excludes the heavy directories."""
    ignored = _dockerignore_matcher()

    for path in (
        "clinicalomx_ml/roi_detection/src/croi/objectives/tma_core/__pycache__/vocab.pyc",
        "clinicalomx_ml/.venv/lib/python3.11/site-packages/torch/__init__.py",
        "clinicalomx_ml/roi_detection/runs/serving/detections.jsonl",
        "clinicalomx_ml/roi_detection/containers/croi-general-inference.sif",
    ):
        assert ignored(path) is not None, path


def test_no_member_pyproject_survives():
    """No member pyproject.toml remains."""
    strays = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "roi_detection").rglob("pyproject.toml")
    )
    assert strays == []


def test_every_package_says_what_belongs_in_it():
    """Every code directory is a package whose `__init__` has a docstring. `configs/` is data."""
    import ast

    croi = ROOT / SRC / "croi"
    undescribed = []
    for directory in sorted(p for p in croi.rglob("*") if p.is_dir()):
        if directory.name in ("__pycache__", "configs"):
            continue
        init = directory / "__init__.py"
        if not init.is_file() or not ast.get_docstring(ast.parse(init.read_text())):
            undescribed.append(str(directory.relative_to(croi)))
    assert undescribed == []
