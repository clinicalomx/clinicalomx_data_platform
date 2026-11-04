"""This repo's declared vocabulary: imports without lamindb, every written name is declared, no
name is declared twice with different dtypes or collides with the foundation's."""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_pipeline_module

vocab = load_pipeline_module("vocab")
qptiff = load_pipeline_module("qptiff")


def test_module_imports_without_lamindb() -> None:
    path = Path(vocab.__file__)
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('probe_vocab', r'{path}');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "assert m.declared_names(), 'no features declared';"
        "banned = [n for n in ('lamindb', 'django', 'prefect', 'clinicalomx') if n in sys.modules];"
        "print('BANNED:' + ','.join(banned)); sys.exit(1 if banned else 0)"
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{r.stdout.strip()} {r.stderr.strip()}"


def test_every_name_is_declared_once() -> None:
    names = vocab.declared_names()
    assert len(names) == len(set(names)), "a feature is declared twice"
    assert len(names) >= 20, f"only {len(names)} declared. Did a block get dropped?"


def test_every_declaration_has_a_description() -> None:
    missing = [name for name, _dtype, desc in vocab._declared() if len(desc.strip()) < 20]
    assert not missing, missing


def test_no_name_collides_with_the_foundation() -> None:
    from clinicalomx.names import ALL_FEATURES

    clash = sorted(set(vocab.declared_names()) & set(ALL_FEATURES))
    assert not clash, f"declared here AND in the foundation: {clash}"


def test_registry_dtypes_stay_sentinels() -> None:
    artifact_named = [n for n, d, _ in vocab._declared()
                      if d is vocab.ARTIFACT or "parquet" in n or "overlay" in n]
    assert artifact_named, "expected some artifact-valued features"
    # datetime is stdlib and needs no sentinel.
    allowed = (vocab.ARTIFACT, str, int, bool, float, dict, datetime.datetime)
    for name, dtype, _desc in vocab._declared():
        assert dtype in allowed or dtype in _declared_types(), (name, dtype)


def test_a_dtype_is_never_a_bare_string_unless_it_names_a_declared_type() -> None:
    offenders = [
        (name, dtype) for name, dtype, _desc in vocab._declared()
        if isinstance(dtype, str) and dtype is not vocab.ARTIFACT
        and dtype not in _declared_types()
    ]
    assert not offenders, (
        f"declared as a string; use the Python type/builtin instead: {offenders}")


def _declared_types() -> set[str]:
    """Every type name a feature dtype may name: ROI subtypes and non-ROI Record types."""
    return set(vocab._declared_roi_types()) | {name for name, _desc in vocab._declared_record_types()}


def test_link_features_declare_the_type_they_name() -> None:
    named = [(n, d) for n, d, _ in vocab._declared()
             if isinstance(d, str) and d is not vocab.ARTIFACT]
    assert named, "expected at least one type-valued link feature"
    for name, type_name in named:
        assert type_name in _declared_types(), (name, type_name)


def test_non_roi_types_stay_out_of_the_roi_umbrella() -> None:
    record_types = {name for name, _desc in vocab._declared_record_types()}
    assert record_types, "expected at least one non-ROI Record type"
    assert not (record_types & set(vocab._declared_roi_types())), (
        "a non-ROI type is also declared as an ROI subtype")
    assert qptiff.QPTIFF_CHANNELS_TYPE in record_types


def test_declared_types_are_framework_roi_types() -> None:
    from clinicalomx import ALL_TYPES

    unknown = [t for t in vocab._declared_roi_types() if t not in ALL_TYPES]
    assert not unknown, f"not framework ROI types: {unknown}"


def test_ensure_is_idempotent_per_process(monkeypatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_vocab_private", vocab.__file__)
    private = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private)
    vocab_ = private

    calls = []
    monkeypatch.setattr(vocab_, "vocabulary", lambda: type("V", (), {
        "ensure": lambda self, *, dry_run=False: calls.append(dry_run) or "rec"})())
    monkeypatch.setattr(vocab_, "_ENSURED", False)
    assert vocab_.ensure() == "rec"
    assert vocab_.ensure() is None  # short-circuited
    assert vocab_.ensure(force=True) == "rec"
    assert vocab_.ensure(dry_run=True) == "rec"  # dry runs always execute
    assert calls == [False, False, True]


@pytest.mark.parametrize("name", [
    "roi_corrected_json", "roi_modified", "roi_thumbnail", "roi_pred",
    "roi_detection_overlay", "roi_dearray_overlay", "roi_accepted", "cores_accepted",
    "general_context", "core_context", "detect_context",
    "qptiff_integrity", "image_type", "qptiff_n_channels", "qptiff_dims", "qptiff_thumbnail",
    "bgnormed", "bgnorm_qc", "bgnorm_attempts", "bgnorm_core_failed",
    "bgnorm_submit_failures", "bgnorm_last_crash_run", "bgnorm_deferred",
    "bgnorm_failed_cores", "bgnorm_failed_channels",
])
def test_known_feature_is_declared(name: str) -> None:
    assert name in vocab.declared_names()


def test_the_state_model_does_not_re_export_feature_names() -> None:
    states = load_pipeline_module("states")
    leaked = [n for n in vocab.declared_names()
              if n in {v for k, v in vars(states).items()
                       if isinstance(v, str) and not k.startswith("_")}]
    assert not leaked, f"states re-exports feature names again: {leaked}"


# Every declarer reaches the aggregate.
def test_every_declarer_in_domain_is_merged_by_vocab() -> None:
    import ast
    import pathlib

    domain = pathlib.Path("src/qptiff_pipeline/domain")
    declaring = set()
    for path in sorted(domain.glob("*.py")):
        if path.name in ("declare.py", "vocab.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("feature", "link", "record_type", "typed")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id.isupper()):
                declaring.add(path.stem)

    merged = {d.module for d in vocab._all()}
    missing = sorted(declaring - merged)
    assert not missing, f"declare features but are not in vocab._MODULES: {missing}"


def test_a_declarers_features_all_reach_declared_names() -> None:
    names = set(vocab.declared_names())
    for d in vocab._all():
        assert d.features, f"{d.module} declares nothing"
        assert {n for n, _dt, _desc in d.features} <= names, d.module
