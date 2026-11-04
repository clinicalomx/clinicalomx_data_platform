"""This repo's declared data invariants, in :mod:`~qptiff_pipeline.checks.pipeline`.

The machinery and the framework's ROI invariants live in :mod:`clinicalomx.checks`. These reach
the runner through the ``clinicalomx.checks`` entry point, so ``python -m clinicalomx.checks run``
covers both.

Checks only read. Bulk reads go through the shared ``Snapshot``, and a failure names each instance.
``flows/data_tests.py`` names these checks explicitly: the SIF does not install this repo, so
entry-point discovery there finds none of them. Checks with ``needs_storage=True`` read stored
bytes and run weekly on the HPC; the rest run daily on the VM.
"""
from __future__ import annotations
