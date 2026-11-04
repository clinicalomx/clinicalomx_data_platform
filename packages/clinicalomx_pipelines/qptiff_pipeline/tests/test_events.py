"""Pipeline code emits through ``orchestration.events.emit`` and nothing else."""
from __future__ import annotations

import ast
import pathlib
import sys
import types

from qptiff_pipeline.orchestration.events import emit

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
BYPASSES = {"prefect_emitter", "register_event_emitter", "emit_event"}


def test_emit_publishes_the_event_unchanged(monkeypatch):
    sent = []
    fake = types.ModuleType("prefect.events")
    fake.emit_event = lambda **kw: sent.append(kw)
    monkeypatch.setitem(sys.modules, "prefect.events", fake)

    emit("clinical-roi.tma.registered", resource={"prefect.resource.id": "x"}, payload={"a": 1})

    assert sent == [{"event": "clinical-roi.tma.registered",
                     "resource": {"prefect.resource.id": "x"}, "payload": {"a": 1}}]


def test_no_module_emits_around_the_seam():
    pkg = SRC / "qptiff_pipeline"
    exempt = {pkg / "orchestration" / "events.py", pkg / "review" / "dev" / "fakes.py"}
    found = []
    for path in SRC.rglob("*.py"):
        if path in exempt:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = (node.id if isinstance(node, ast.Name)
                    else node.attr if isinstance(node, ast.Attribute)
                    else None)
            if name in BYPASSES:
                found.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert not found, found
