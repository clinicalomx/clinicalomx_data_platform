"""Tests for resolving a declared dtype to the form lamindb stores, including string dtypes such
as ``cat[bionty.Protein]``. Writes nothing. Skipped unless a test instance is connected::

    CLINICALOMXDB_SCHEMA_TEST_INSTANCE=1 uv run pytest tests/test_vocab_dtypes.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLINICALOMXDB_SCHEMA_TEST_INSTANCE"),
    reason="needs a connected LaminDB instance",
)


def test_a_canonical_string_passes_through_unchanged():
    from clinicalomx.vocab.upsert import declared_dtype

    assert declared_dtype("cat[bionty.Protein]") == "cat[bionty.Protein]"
    assert declared_dtype("cat[Record]") == "cat[Record]"


@pytest.mark.parametrize("scalar", ["str", "int", "float", "bool"])
def test_scalar_strings_pass_through(scalar):
    from clinicalomx.vocab.upsert import declared_dtype

    assert declared_dtype(scalar) == scalar


def test_a_string_and_its_registry_class_agree():
    import bionty as bt
    import lamindb as ln

    from clinicalomx.vocab.upsert import declared_dtype

    assert declared_dtype("cat[bionty.Protein]") == declared_dtype(bt.Protein)
    assert declared_dtype("cat[Record]") == declared_dtype(ln.Record)


def test_a_string_that_is_not_a_dtype_raises():
    from clinicalomx.vocab.upsert import declared_dtype

    with pytest.raises(ValueError, match="has to be one of"):
        declared_dtype("Record")

    with pytest.raises(ValueError, match="has to be one of"):
        declared_dtype("not_a_dtype")


def test_a_dangling_pin_is_returned_verbatim_and_never_parsed():
    from clinicalomx.vocab.dataio import LiteralDtype
    from clinicalomx.vocab.upsert import declared_dtype

    pin = LiteralDtype("cat[Record[abc123xyz]]")
    assert declared_dtype(pin) == "cat[Record[abc123xyz]]"
