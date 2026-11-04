"""Tests for the check runner, consumer discovery, and the read-only guarantee."""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

from clinicalomx import checks

CHECKS_DIR = pathlib.Path(checks.__file__).parent
SRC = CHECKS_DIR.parent

#: Every module of the read-only layer.
GUARDED = (SRC / "snapshot.py", CHECKS_DIR, SRC / "extract")

#: The one module permitted to write.
WRITE_ALLOWLIST = frozenset({SRC / "extract" / "publish.py"})


def _guarded_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for target in GUARDED:
        if target.is_dir():
            out += sorted(target.rglob("*.py"))
        elif target.exists():
            out.append(target)
    return out


def _check(name="c", severity="error", failures=(), needs_storage=False, raises=None):
    def test(_snap):
        if raises is not None:
            raise raises
        return list(failures)

    return checks.Check(name, severity, test, needs_storage=needs_storage)


def test_a_check_with_no_failures_passes():
    (r,) = checks.run_checks([_check(failures=[])])
    assert r.passed and r.failures == []


def test_failures_are_reported_verbatim():
    (r,) = checks.run_checks([_check(failures=["a.parquet", "b.png"])])
    assert not r.passed
    assert r.failures == ["a.parquet", "b.png"]


def test_a_raising_check_becomes_a_failure_not_a_crash():
    boom, fine = checks.run_checks([_check(name="boom", raises=ValueError("nope")),
                                    _check(name="fine", failures=[])])
    assert not boom.passed and "ValueError" in boom.failures[0]
    assert fine.passed, "a sibling check still ran"


def test_storage_checks_are_skipped_without_byte_access():
    cs = [_check(name="meta"), _check(name="bytes", needs_storage=True)]
    assert [r.check.name for r in checks.run_checks(cs)] == ["meta"]
    assert [r.check.name for r in checks.run_checks(cs, include_storage=True)] == ["meta", "bytes"]


@pytest.mark.parametrize(("severity", "expect_failed"), [("error", True), ("warn", False)])
def test_only_error_severity_fails_the_run(severity, expect_failed):
    results = checks.run_checks([_check(severity=severity, failures=["x"])])
    _report, failed = checks.summarise(results)
    assert failed is expect_failed


def test_summary_truncates_but_says_how_many_were_hidden():
    results = checks.run_checks([_check(failures=[f"f{i}" for i in range(12)])])
    report, _ = checks.summarise(results, max_examples=3)
    assert "and 9 more" in report
    assert report.count("\n            f") == 3


def test_summary_puts_errors_before_warnings():
    results = checks.run_checks([_check(name="z_warn", severity="warn", failures=["w"]),
                                 _check(name="a_error", severity="error", failures=["e"])])
    report, _ = checks.summarise(results)
    assert report.index("a_error") < report.index("z_warn")


def test_one_snapshot_is_shared_across_checks():
    seen = []
    cs = [checks.Check(f"c{i}", "error", lambda s: seen.append(s) or []) for i in range(3)]
    checks.run_checks(cs, snapshot=checks.Snapshot())
    assert len({id(s) for s in seen}) == 1


# The Snapshot re-export.
def test_snapshot_is_re_exported_as_the_same_object():
    from clinicalomx import snapshot

    assert checks.Snapshot is snapshot.Snapshot


# Consumer discovery.
class _EP:
    def __init__(self, name, value):
        self.name, self._value = name, value

    def load(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def test_discover_collects_each_consumer_s_checks(monkeypatch):
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_EP("repo-a", lambda: [_check(name="a.one")])],
    )
    assert {o: [c.name for c in cs] for o, cs in checks.discover().items()} == {
        "repo-a": ["a.one"]}


def test_a_broken_consumer_does_not_break_the_run(monkeypatch):
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_EP("broken", ImportError("no")),
                       _EP("fine", lambda: [_check(name="f.one")])],
    )
    found = checks.discover()
    assert "broken" not in found
    assert [c.name for c in found["fine"]] == ["f.one"]


def test_non_check_objects_are_filtered_out(monkeypatch):
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_EP("sloppy", lambda: [_check(name="ok"), "not a check"])],
    )
    assert [c.name for c in checks.discover()["sloppy"]] == ["ok"]


# The read-only guarantee.
# Method names that only mutate, whatever the receiver.
LAMINDB_WRITES = {"save", "delete", "add_values", "remove_values", "set_values", "restore", "trash"}
# clinicalomx ROI writes, matched only on a `cx.`/`clinicalomx.`/`roi.` receiver.
FRAMEWORK_WRITES = {"annotate", "split", "merge", "create", "update"}
FRAMEWORK_RECEIVERS = {"cx", "clinicalomx", "roi"}


def _write_calls(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr, recv = node.func.attr, node.func.value
        if attr in LAMINDB_WRITES or (attr in FRAMEWORK_WRITES and isinstance(recv, ast.Name)
              and recv.id in FRAMEWORK_RECEIVERS):
            hits.append(node.lineno)
    return hits


def test_no_read_only_module_writes_to_the_instance():
    offenders = []
    for path in _guarded_files():
        if path in WRITE_ALLOWLIST:
            continue
        lines = path.read_text().splitlines()
        offenders += [f"{path.relative_to(SRC)}:{n}  {lines[n - 1].strip()}"
                      for n in _write_calls(ast.parse(path.read_text()))]
    assert not offenders, "a read-only module writes to the instance:\n" + "\n".join(offenders)


def _clinicalomx_imports(tree: ast.AST) -> set[str]:
    """``clinicalomx.x.y`` module paths this file imports, however it is spelled."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("clinicalomx"):
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("clinicalomx")}
    return found


def test_the_guarded_set_covers_every_bulk_reader_the_layer_reaches():
    guarded = {p.resolve() for p in _guarded_files()}
    reached: set[str] = set()
    for path in _guarded_files():
        if path not in WRITE_ALLOWLIST:
            reached |= _clinicalomx_imports(ast.parse(path.read_text()))

    offenders = []
    for dotted in sorted(reached):
        parts = dotted.split(".")[1:]
        if not parts:
            continue  # a bare `import clinicalomx`: the lazy facade
        rel = pathlib.Path(*parts)
        for candidate in (SRC / rel.with_suffix(".py"), SRC / rel / "__init__.py"):
            if not candidate.exists() or candidate.resolve() in guarded:
                continue
            if "to_dataframe" in candidate.read_text():
                offenders.append(str(candidate.relative_to(SRC)))
    assert not offenders, (
        f"bulk-reading modules reachable from the read-only layer but outside GUARDED: "
        f"{sorted(set(offenders))}")


def test_the_scope_guard_would_notice_an_unguarded_bulk_reader():
    stray = SRC / "_stray_bulk_reader.py"
    stray.write_text("def read():\n    return ln.Record.filter().to_dataframe(limit=10)\n")
    try:
        tree = ast.parse("from clinicalomx._stray_bulk_reader import read\n")
        assert "clinicalomx._stray_bulk_reader" in _clinicalomx_imports(tree)
        assert stray.resolve() not in {p.resolve() for p in _guarded_files()}
        assert "to_dataframe" in stray.read_text()
    finally:
        stray.unlink()


def test_the_write_guard_would_catch_a_mutation():
    assert _write_calls(ast.parse("record.features.add_values({'x': 1})"))
    assert _write_calls(ast.parse("artifact.delete(permanent=True)"))
    assert _write_calls(ast.parse("cx.annotate(roi, art, 'role')"))
    assert _write_calls(ast.parse("roi.split(art, kids, 'role', parent=p)"))
    # Not writes: same method names, different receivers.
    assert not _write_calls(ast.parse('key.split("/")[1]'))
    assert not _write_calls(ast.parse("counts.update({'a': 1})"))
    assert not _write_calls(ast.parse("gdf.attrs.get('scale_factor')"))


def test_importing_the_checks_does_not_connect_to_a_database():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import clinicalomx.checks, clinicalomx.checks.roi, sys;"
         "print('yes' if 'lamindb' in sys.modules else '')"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "importing the checks pulled in lamindb (and so connected)"


def test_the_cli_help_works_without_a_database():
    proc = subprocess.run(
        [sys.executable, "-m", "clinicalomx.checks", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "run" in proc.stdout and "list" in proc.stdout



def test_every_bulk_read_passes_an_explicit_limit():
    offenders = []
    for path in _guarded_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "to_dataframe":
                continue
            if not any(kw.arg == "limit" for kw in node.keywords):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f"to_dataframe without an explicit limit: {offenders}"


