"""Each objective's record checked against its configs: every stored key sits under `lamin_key_prefix`."""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import sys
import types

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"
OBJECTIVES = {"tissue_objects": "TISSUE_OBJECT_DETECTION", "tma_core": "TMA_CORE_OBJECT_DETECTION"}

#: Keys under `clinical-roi/` that belong to no single objective.
SHARED_PREFIX = "clinical-roi/shared/"

#: Objective-owned keys stored outside the prefix. Renaming one means re-registering it.
OUTSIDE_PREFIX = {"tissue_objects": {"clinical-roi/thumbnails"}, "tma_core": set()}


class _Stub(types.ModuleType):
    """Any attribute is a fresh class: enough for dtype sentinels and uncalled decorators."""

    def __getattr__(self, name: str) -> type:
        cls = type(name, (), {})
        setattr(self, name, cls)
        return cls


class _Vocabulary:
    def __init__(self, owner: str):
        self.owner = owner

    def feature(self, name: str, *args, **kwargs) -> str:
        return name

    def type(self, name: str, *args, **kwargs) -> str:
        return name


def _record(monkeypatch, obj: str):
    """The objective's record, imported from `src/` with lamindb and clinicalomx stubbed."""
    clinicalomx = _Stub("clinicalomx")
    clinicalomx.DEFAULT_ALIAS = "production"
    vocab = types.ModuleType("clinicalomx.vocab")
    vocab.Vocabulary = _Vocabulary
    clinicalomx.vocab = vocab
    for name, module in (("lamindb", _Stub("lamindb")), ("clinicalomx", clinicalomx),
                         ("clinicalomx.vocab", vocab)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(SRC))
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    module = importlib.import_module(f"croi.objectives.{obj}.objective")
    return getattr(module, OBJECTIVES[obj])


def _strings(node) -> list[str]:
    """Every string value in a parsed yaml document."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _keys(obj: str) -> set[str]:
    """Every `clinical-roi/...` string the objective's configs carry."""
    out = set()
    for path in (SRC / "croi/objectives" / obj / "configs").glob("*.yaml"):
        out |= {s for s in _strings(yaml.safe_load(path.read_text()))
                if s.startswith("clinical-roi/")}
    return out


def _under(key: str, prefix: str) -> bool:
    """`prefix` itself, a key below it, or an MLflow experiment named after it."""
    return key == prefix or key.startswith((prefix + "/", prefix + "-"))


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_every_config_key_uses_the_objectives_prefix(monkeypatch, obj: str):
    record = _record(monkeypatch, obj)
    keys = _keys(obj)
    assert any(_under(k, record.lamin_key_prefix) for k in keys), "no key uses the prefix at all"
    stray = sorted(k for k in keys
                   if not _under(k, record.lamin_key_prefix)
                   and not k.startswith(SHARED_PREFIX)
                   and k not in OUTSIDE_PREFIX[obj])
    assert stray == [], f"{obj} configs use keys outside {record.lamin_key_prefix!r}: {stray}"


def test_no_two_objectives_share_a_prefix(monkeypatch):
    prefixes = [_record(monkeypatch, obj).lamin_key_prefix for obj in OBJECTIVES]
    assert len(set(prefixes)) == len(prefixes)


def test_the_lookup_names_every_objective_directory(monkeypatch):
    """`croi.objectives.get` resolves every objective directory."""
    monkeypatch.syspath_prepend(str(SRC))
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    names = importlib.import_module("croi.objectives").NAMES
    dirs = {p.name for p in (SRC / "croi/objectives").iterdir()
            if p.is_dir() and p.name != "__pycache__"}
    assert set(names) == dirs == set(OBJECTIVES)


#: The modules `croi.io.objectives` says an objective implements.
TEMPLATE_MODULES = ("objective.py", "registry.py", "environment.py", "vocab.py", "config.py",
                    "dataset.py", "cli.py")


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_each_objective_implements_every_part_of_the_template(monkeypatch, obj: str):
    """A record that names its own directory, a concrete dataset, and a vocabulary to bootstrap."""
    root = SRC / "croi/objectives" / obj
    missing = [m for m in TEMPLATE_MODULES if not (root / m).is_file()]
    assert missing == [], f"{obj} lacks {missing}"

    record = _record(monkeypatch, obj)
    template = importlib.import_module("croi.io.datasets")
    assert record.name == obj, "`croi.objectives.get` and the shared stages find it by this name"
    assert issubclass(record.dataset, template.DetectionDataset)
    assert not inspect.isabstract(record.dataset), f"{obj}'s dataset leaves a template method out"
    vocab = record.vocabulary()
    assert vocab.VOCAB.owner.startswith("croi-") and vocab.ROI_SCALARS


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_each_environment_binds_its_own_directory(obj: str):
    """`ENVIRONMENT` names its own objective, not a sibling's."""
    tree = ast.parse((SRC / "croi/objectives" / obj / "environment.py").read_text())
    bound = [node.args[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ObjectiveEnvironment"]
    assert bound == [obj]


def test_the_shared_stages_are_every_objectives_and_none_shadows_them(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC))
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    cli = importlib.import_module("croi.cli")
    for obj in OBJECTIVES:
        assert set(cli.SHARED_SUBCOMMANDS) <= set(cli.subcommands(obj))

    own = importlib.import_module("croi.objectives.tma_core.cli")
    monkeypatch.setitem(own.SUBCOMMANDS, "train", cli.SHARED_SUBCOMMANDS["train"])
    with pytest.raises(ValueError, match="redefines the shared stages"):
        cli.subcommands("tma_core")
