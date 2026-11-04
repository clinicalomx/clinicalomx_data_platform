"""No inference-side module of this package imports mlflow or a training module.

Covers this package's own import graph only; mlflow is still installed in every image through
``clinicalomx[models]``. Reads source with ``ast``, so it needs no install.
"""
from __future__ import annotations

import ast
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = ROOT / "roi_detection"


TISSUE = "croi.objectives.tissue_objects"
CORE = "croi.objectives.tma_core"


def roots(base: pathlib.Path | None = None) -> dict[str, pathlib.Path]:
    """``{dotted prefix: root directory}``, longest prefix wins. ``base`` re-roots under a temp dir."""
    b = base if base is not None else PKG
    return {
        TISSUE: b / "src/croi/objectives/tissue_objects",
        CORE: b / "src/croi/objectives/tma_core",
        "croi": b / "src/croi",
    }


#: Where each closure walk starts: what `clinicalomx_pipelines` imports. The layer-wide test
#: covers every other inference module.
ENTRY_POINTS = {
    TISSUE: (f"{TISSUE}.registry",),
    CORE: (f"{CORE}.registry",),
    "croi": ("croi.detection.detector", "croi.detection.backends", "croi.io.registries"),
}

PACKAGE_NAMES = (TISSUE, CORE, "croi")

#: Each area's training stack. A new module is inference-side unless added here.
TISSUE_TRAIN_MODULES: frozenset[str] = frozenset()

CORE_TRAIN_MODULES: frozenset[str] = frozenset()

#: The shared layer's training stack: modules that import mlflow or drive one that does.
CROI_TRAIN_MODULES = frozenset({
    "croi.train",
    "croi.cli",
    "croi.__main__",
    "croi.evaluation.evaluate",
    "croi.objectives.tissue_objects.flows.tissue_objects_train",
    "croi.objectives.tma_core.flows.tma_core_train",
    "croi.detection.backends.rfdetr.trainer",
    "croi.detection.backends.ultralytics.mlflow_callbacks",
    "croi.detection.backends.ultralytics.trainer",
    "croi.detection.packaging",
    "croi.detection.tracking",
    "croi.detection.backends.training",
    "croi.utils.fetch_weights",
})

TRAIN_MODULES = {TISSUE: TISSUE_TRAIN_MODULES, CORE: CORE_TRAIN_MODULES, "croi": CROI_TRAIN_MODULES}

#: `(area, dotted name)` of inference-side modules allowed to import mlflow. Empty.
KNOWN_MLFLOW_IN_INFERENCE: set[tuple[str, str]] = set()


def _module_path(dotted: str, rs: dict[str, pathlib.Path] | None = None) -> pathlib.Path | None:
    """The file for a dotted name, at any depth below the root owning its longest prefix."""
    rs = rs if rs is not None else roots()
    prefix = max((p for p in rs if dotted == p or dotted.startswith(p + ".")),
                 key=len, default=None)
    if prefix is None:
        return None
    root = rs[prefix]
    tail = dotted[len(prefix):].lstrip(".")
    if not tail:
        return root / "__init__.py" if (root / "__init__.py").is_file() else None
    for candidate in (root / f"{pathlib.Path(*tail.split('.'))}.py",
                      root / pathlib.Path(*tail.split(".")) / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _owner(dotted: str, rs: dict[str, pathlib.Path] | None = None) -> str | None:
    """The area a dotted name belongs to: the longest declared prefix of it."""
    rs = rs if rs is not None else roots()
    return max((p for p in rs if dotted == p or dotted.startswith(p + ".")), key=len, default=None)


def _package_of(path: pathlib.Path, dotted: str) -> str:
    """The package a relative import in this file resolves against. An `__init__.py` is its own."""
    return dotted if path.name == "__init__.py" else dotted.rsplit(".", 1)[0]


def _ancestor_packages(dotted: str, rs: dict[str, pathlib.Path] | None = None) -> list[str]:
    """Every package ``import <dotted>`` executes on the way in, from ``croi`` down."""
    parts = dotted.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _dotted_for(path: pathlib.Path, rs: dict[str, pathlib.Path] | None = None) -> str:
    """The dotted name of a file, resolved against whichever root contains it."""
    rs = rs if rs is not None else roots()
    for prefix, root in sorted(rs.items(), key=lambda kv: len(str(kv[1])), reverse=True):
        if path.is_relative_to(root):
            rel = path.relative_to(root)
            tail = list(rel.parts[:-1]) if rel.name == "__init__.py" else [*rel.parts[:-1], rel.stem]
            return ".".join([prefix, *tail]) if tail else prefix
    raise ValueError(f"{path} is under no declared root")


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    """Resolve one `from ... import ...` against the importing module's package."""
    if not node.level:
        return node.module
    parts = package.split(".")
    keep = len(parts) - node.level + 1
    if keep <= 0:
        return None
    base = parts[:keep]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _imports(path: pathlib.Path, package: str) -> set[str]:
    """Every dotted name this module imports, including inside functions.

    An `ImportFrom` yields both the module and ``module.alias`` for each name.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            if not module:
                continue
            if node.module:
                names.add(module)
            names.update(f"{module}.{a.name}" for a in node.names if a.name != "*")
    return names


def _module_imports(path: pathlib.Path, rs: dict[str, pathlib.Path] | None = None) -> set[str]:
    """`_imports` for any file under a declared root, its package read off its position."""
    dotted = _dotted_for(path, rs)
    return _imports(path, _package_of(path, dotted))


def _closure(obj: str, rs: dict[str, pathlib.Path] | None = None) -> dict[str, set[str]]:
    """First-party import closure of one area's inference entry points, as ``{module: imports}``."""
    rs = rs if rs is not None else roots()
    seen: dict[str, set[str]] = {}
    queue = list(ENTRY_POINTS[obj])
    while queue:
        dotted = queue.pop()
        if dotted in seen or (path := _module_path(dotted, rs)) is None:
            continue
        seen[dotted] = _imports(path, _package_of(path, dotted))
        queue += _ancestor_packages(dotted, rs)
        queue += [n for n in seen[dotted] if n.split(".")[0] == "croi"]
    return seen


def _inference_modules(obj: str, rs: dict[str, pathlib.Path] | None = None) -> dict[str, pathlib.Path]:
    """``{dotted: file}`` for every inference-side module of one area, owned by longest prefix."""
    rs = rs if rs is not None else roots()
    root = rs.get(obj)
    out = {}
    if root is None or not root.is_dir():
        return out
    for f in sorted(root.rglob("*.py")):
        dotted = _dotted_for(f, rs)
        if _owner(dotted, rs) == obj and not _is_train_import(dotted):
            out[dotted] = f
    return out


def _layer_offenders(
    obj: str,
    rs: dict[str, pathlib.Path] | None = None,
    exempt: set[tuple[str, str]] = KNOWN_MLFLOW_IN_INFERENCE,
) -> dict[str, list[str]]:
    """Every inference-side module of one area that crosses the boundary, keyed by dotted name."""
    rs = rs if rs is not None else roots()
    offenders = {}
    for dotted, f in _inference_modules(obj, rs).items():
        bad = _offenders(_module_imports(f, rs), mlflow=(obj, dotted) not in exempt)
        if bad:
            offenders[dotted] = bad
    return offenders


def _is_train_import(dotted: str) -> bool:
    """Whether the name belongs to any area's training stack."""
    return any(dotted == m or dotted.startswith(m + ".")
               for names in TRAIN_MODULES.values() for m in names)


def _offenders(imports: set[str], *, mlflow: bool = True) -> list[str]:
    """The imports in one module that cross the boundary. An `ImportFrom` may report twice."""
    bad = [n for n in imports if _is_train_import(n)]
    if mlflow:
        bad += [n for n in imports if n.split(".")[0] == "mlflow"]
    return sorted(bad)


@pytest.mark.parametrize("obj", PACKAGE_NAMES)
def test_the_inference_entry_points_reach_no_mlflow(obj: str):
    offenders = {
        mod: sorted(n for n in imports if n.split(".")[0] == "mlflow")
        for mod, imports in _closure(obj).items()
        if any(n.split(".")[0] == "mlflow" for n in imports)
    }
    assert offenders == {}


@pytest.mark.parametrize("obj", PACKAGE_NAMES)
def test_the_inference_entry_points_reach_no_train_module(obj: str):
    offenders = {
        mod: sorted(n for n in imports if _is_train_import(n))
        for mod, imports in _closure(obj).items()
        if any(_is_train_import(n) for n in imports)
    }
    assert offenders == {}


@pytest.mark.parametrize("obj", PACKAGE_NAMES)
def test_no_inference_module_at_all_takes_a_train_import(obj: str):
    """Every inference-side module, not only what the entry points reach."""
    assert _layer_offenders(obj) == {}


@pytest.mark.parametrize("obj", PACKAGE_NAMES)
def test_the_train_set_is_exactly_the_modules_that_reach_mlflow(obj: str):
    """A train-set member must reach mlflow, by import or by driving another member."""
    rs = roots()
    for dotted in sorted(TRAIN_MODULES[obj]):
        path = _module_path(dotted, rs)
        assert path is not None, f"{dotted} is in {obj}'s train set and does not exist"
        imports = _imports(path, _package_of(path, dotted))
        assert any(n.split(".")[0] == "mlflow" or _is_train_import(n) for n in imports), (
            f"{dotted} no longer reaches mlflow; move it out of {obj}'s train set"
        )


def test_the_two_train_sets_are_mirror_images():
    """Both objectives have the same train set."""
    assert {n.removeprefix(TISSUE) for n in TISSUE_TRAIN_MODULES} == \
           {n.removeprefix(CORE) for n in CORE_TRAIN_MODULES}


def test_every_mlflow_exemption_is_still_earned():
    """An exemption that is no longer needed fails."""
    rs = roots()
    for obj, dotted in sorted(KNOWN_MLFLOW_IN_INFERENCE):
        path = _module_path(dotted, rs)
        assert path is not None, f"{dotted} is exempted and does not exist"
        assert any(n.split(".")[0] == "mlflow" for n in _module_imports(path, rs)), (
            f"{dotted} no longer imports mlflow; drop it from KNOWN_MLFLOW_IN_INFERENCE"
        )


def test_the_closure_is_not_silently_empty():
    """Every closure is non-empty."""
    for obj in PACKAGE_NAMES:
        closure = _closure(obj)
        assert set(ENTRY_POINTS[obj]) <= set(closure), obj
        followed = {n for imports in closure.values() for n in imports if n in closure}
        assert followed, f"{obj}: no first-party import edge was resolved"
        if obj != "croi":
            assert "croi.io.registries" in closure, f"{obj}: the walk never reached croi"
        assert len(_inference_modules(obj)) > 5, f"{obj}: the inference-side scan found nothing"


#: An uninvolved inference-side module per area, to plant an import into a copy of.
_VICTIM = {TISSUE: f"{TISSUE}.thumbnail", CORE: f"{CORE}.crops", "croi": "croi.utils.roi_records"}


@pytest.mark.parametrize("obj", PACKAGE_NAMES)
def test_a_planted_offender_is_caught(tmp_path, obj: str):
    """The empty exemption list still detects a violation, planted in a copy of the real tree."""
    rs = roots(tmp_path)
    shutil.copytree(roots()[obj], rs[obj])
    dotted = _VICTIM[obj]
    victim = _module_path(dotted, rs)
    assert victim is not None, f"{dotted} does not exist; pick another victim"
    victim.write_text("import mlflow\nfrom croi.train import train_model\n" + victim.read_text())

    offenders = _layer_offenders(obj, rs)
    assert offenders.get(dotted) == ["croi.train", "croi.train.train_model", "mlflow"]
    assert _layer_offenders(obj, rs, exempt={(obj, dotted)}) == {
        dotted: ["croi.train", "croi.train.train_model"]
    }, "an exemption forgives mlflow and never a training import"


def test_the_walk_reaches_a_module_nested_below_the_root(tmp_path):
    """A subpackage below an objective must resolve."""
    rs = roots(tmp_path)
    root = rs[TISSUE]
    (root / "backends").mkdir(parents=True)
    (root / "registry.py").write_text(f"from {TISSUE}.backends.champion import load\n")
    (root / "backends/__init__.py").write_text("from croi.evaluation.evaluate import log\n")
    (root / "backends/champion.py").write_text("import mlflow\n")

    closure = _closure(TISSUE, rs)

    assert f"{TISSUE}.backends" in closure, "a subpackage `__init__.py` must resolve"
    assert f"{TISSUE}.backends.champion" in closure, "a deeper name must resolve"
    assert _offenders(closure[f"{TISSUE}.backends.champion"]) == ["mlflow"]
    assert _offenders(closure[f"{TISSUE}.backends"]) == [
        "croi.evaluation.evaluate", "croi.evaluation.evaluate.log",
    ]
    assert _dotted_for(root / "backends/champion.py", rs) == f"{TISSUE}.backends.champion"


def test_the_walk_follows_the_two_import_forms_a_name_match_would_miss(tmp_path):
    """`from X import mod` names the module in the ALIAS, and `from .mod import x` is relative."""
    rs = roots(tmp_path)
    root = rs[TISSUE]
    (root / "data").mkdir(parents=True)
    (root / "registry.py").write_text(
        f"from {TISSUE}.data import roi_records\nfrom .helpers import resolve_weights\n")
    (root / "data/roi_records.py").write_text("import mlflow\n")
    (root / "helpers.py").write_text("from croi import train\n")

    closure = _closure(TISSUE, rs)

    assert f"{TISSUE}.data.roi_records" in closure, "the two-part `from X import mod` form"
    assert f"{TISSUE}.helpers" in closure, "the relative `from .mod import name` form"
    assert _offenders(closure[f"{TISSUE}.data.roi_records"]) == ["mlflow"]
    assert _offenders(closure[f"{TISSUE}.helpers"]) == ["croi.train"]  # module alias


def test_the_layer_walk_resolves_a_relative_import_in_a_package_init(tmp_path):
    """An `__init__.py` is its own package."""
    rs = roots(tmp_path)
    root = rs[TISSUE]
    (root / "data").mkdir(parents=True)
    (root / "__init__.py").write_text("from ... import train\n")
    (root / "data/__init__.py").write_text("from ....evaluation import evaluate\n")
    (root / "data/split.py").write_text("from . import roi_records\n")

    assert _layer_offenders(TISSUE, rs) == {
        TISSUE: ["croi.train"],
        f"{TISSUE}.data": ["croi.evaluation.evaluate"],
    }


def test_the_closure_visits_the_packages_an_import_executes(tmp_path):
    """`import <objective>.registry` runs the objective's `__init__` first, so the walk must too."""
    rs = roots(tmp_path)
    root = rs[TISSUE]
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("import mlflow\n")
    (root / "registry.py").write_text("x = 1\n")

    closure = _closure(TISSUE, rs)

    assert TISSUE in closure
    assert _offenders(closure[TISSUE]) == ["mlflow"]


def test_the_layer_walk_refuses_a_training_import(tmp_path, monkeypatch):
    """The walk consults every area's train set, the other objective's included."""
    monkeypatch.setitem(TRAIN_MODULES, TISSUE, frozenset({f"{TISSUE}.trainer"}))
    rs = roots(tmp_path)
    root = rs[CORE]
    root.mkdir(parents=True)
    (root / "gridding.py").write_text("from croi.train import train_model\n")
    (root / "dearray.py").write_text(f"import {TISSUE}.trainer as t\n")

    assert _layer_offenders(CORE, rs) == {
        f"{CORE}.dearray": [f"{TISSUE}.trainer"],
        f"{CORE}.gridding": ["croi.train", "croi.train.train_model"],
    }


def test_the_closure_refuses_a_training_import(tmp_path):
    """Same on the production path: an objective's `registry` reaching the trainer."""
    rs = roots(tmp_path)
    root = rs[CORE]
    root.mkdir(parents=True)
    (root / "registry.py").write_text("from croi.train import train_model\n")

    offenders = {
        mod: sorted(n for n in imports if _is_train_import(n))
        for mod, imports in _closure(CORE, rs).items()
        if any(_is_train_import(n) for n in imports)
    }

    assert offenders == {f"{CORE}.registry": ["croi.train", "croi.train.train_model"]}


def _imports_of(owner: str, others: tuple[str, ...]) -> dict[str, list[str]]:
    """`{module: names it imports under any of others}` for every module ``owner`` owns."""
    rs = roots()
    out = {}
    for f in sorted(rs[owner].rglob("*.py")):
        dotted = _dotted_for(f, rs)
        if _owner(dotted, rs) != owner:
            continue
        names = sorted(n for n in _module_imports(f, rs)
                       if any(n == o or n.startswith(o + ".") for o in others))
        if names:
            out[dotted] = names
    return out


def test_the_shared_layer_imports_no_objective():
    """Nothing in `croi` imports an objective."""
    assert _imports_of("croi", (TISSUE, CORE)) == {}


def test_no_objective_imports_the_other():
    assert _imports_of(TISSUE, (CORE,)) == {}
    assert _imports_of(CORE, (TISSUE,)) == {}
