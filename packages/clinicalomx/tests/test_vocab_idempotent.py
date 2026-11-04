"""Applying the vocabulary twice changes nothing the second time. Skipped unless a throwaway
instance is connected::

    CLINICALOMXDB_SCHEMA_TEST_INSTANCE=1 pytest tests/test_idempotent.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLINICALOMXDB_SCHEMA_TEST_INSTANCE"),
    reason="needs a connected throwaway LaminDB instance",
)


def test_apply_twice_is_a_noop():
    from clinicalomx.vocab.__main__ import run

    run(dry_run=False)
    second = run(dry_run=False)

    assert not second.pending_changes, (
        "second apply still wants to change things:\n"
        + "\n".join(f"  {c}" for c in second.pending_changes)
    )


def test_plan_is_empty_after_apply():
    from clinicalomx.vocab.__main__ import run

    run(dry_run=False)
    plan = run(dry_run=True)
    assert not plan.pending_changes
