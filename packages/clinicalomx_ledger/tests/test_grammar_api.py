"""The grammar's doctests run, and importing it loads neither Django nor lamindb."""

from __future__ import annotations

import doctest
import subprocess
import sys

import pytest

from clinicalomx_ledger.grammar import example, gate, lineage, model, store

#: Modules a cold grammar import must not load.
_NEEDS_A_DATABASE = ("django", "lamindb", "psycopg2", "clinicalomx_ledger.models")

_MODULES = (model, gate, store, lineage, example)

#: Modules carrying doctests. `store`'s run against `MemoryClaimStore`.
_DOCUMENTED = (model, store)


@pytest.mark.parametrize("module", _DOCUMENTED, ids=lambda m: m.__name__)
def test_the_examples_still_work(module):
    failed, attempted = doctest.testmod(module, verbose=False)
    assert attempted, f"{module.__name__} is listed here but carries no example"
    assert failed == 0, f"{failed} of {attempted} examples in {module.__name__} failed"


@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__)
def test_the_grammar_imports_without_a_database(module):
    """Checked in a subprocess, since this session already imported lamindb."""
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import {module.__name__}, sys;"
         f"print(','.join(m for m in {_NEEDS_A_DATABASE!r} if m in sys.modules))"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"importing {module.__name__} pulled in {proc.stdout.strip()}"
    )
