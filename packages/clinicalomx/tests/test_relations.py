"""Tests for element relations: tables annotate label images, and an unrecorded derivation stays
``None``."""
from __future__ import annotations

import json

import pytest

from clinicalomx.extract import relations as rel


class _Snap:
    """Only `artifact_provenance` is read; an absent key has no recorded source."""

    def __init__(self, provenance=None):
        self._p = provenance or {}

    def artifact_provenance(self, keys):
        return {k: self._p[k] for k in keys if k in self._p}


# Derivation, observed.
def test_a_labels_element_is_linked_to_the_image_it_was_derived_from():
    placed = [("bgnorm", "image", "a/bgnorm.zarr"),
              ("labels", "labels", "a/seg.zarr")]
    out = rel.resolve("roi", placed, _Snap({"a/seg.zarr": ("a/bgnorm.zarr", "source_artifact")}))
    assert out.derived_from("labels") == "bgnorm"
    assert out.edges[0].evidence == "source_artifact"


def test_a_labels_element_with_no_recorded_lineage_is_null_not_guessed():
    placed = [("bgnorm", "image", "a/bgnorm.zarr"),
              ("labels", "labels", "a/seg.zarr")]
    out = rel.resolve("roi", placed, _Snap())
    assert out.derived_from("labels") is None
    assert out.unresolved
    assert "NOT guessed" in " ".join(out.caveats())


def test_different_cores_can_derive_from_different_variants():
    placed = [("c0_raw", "image", "raw.qptiff"), ("c0_labels", "labels", "c0/seg.zarr"),
              ("c1_bgnorm", "image", "c1/bgnorm.zarr"), ("c1_labels", "labels", "c1/seg.zarr")]
    out = rel.resolve("roi", placed, _Snap({
        "c0/seg.zarr": ("raw.qptiff", "source_artifact"),
        "c1/seg.zarr": ("c1/bgnorm.zarr", "run_lineage")}))
    assert out.derived_from("c0_labels") == "c0_raw"
    assert out.derived_from("c1_labels") == "c1_bgnorm"


def test_lineage_to_an_artifact_this_object_reads_twice_is_ambiguous_not_a_coin_flip():
    placed = [("bgnorm", "image", "a/bgnorm.zarr"), ("bgnormQ", "image", "a/bgnorm.zarr"),
              ("labels", "labels", "a/seg.zarr")]
    out = rel.resolve("roi", placed, _Snap({"a/seg.zarr": ("a/bgnorm.zarr", "source_artifact")}))
    edge = out.edges[0]
    assert edge.source is None, "one of two equally-likely elements was named as fact"
    assert edge.candidates == ("bgnorm", "bgnormQ")
    assert "coin flip" in " ".join(out.caveats())


def test_lineage_pointing_outside_the_object_is_distinguished_from_nothing_known():
    placed = [("bgnorm", "image", "a/bgnorm.zarr"), ("labels", "labels", "a/seg.zarr")]
    out = rel.resolve("roi", placed, _Snap({"a/seg.zarr": ("elsewhere/other.zarr", "run_lineage")}))
    assert out.edges[0].source is None
    assert "outside-object" in out.edges[0].evidence


def test_an_object_with_no_labels_has_no_derivation_edges():
    placed = [("bgnorm", "image", "a/bgnorm.zarr")]
    assert rel.resolve("roi", placed, _Snap()).edges == []


# The record itself.
def test_every_edge_states_how_it_was_learned():
    placed = [("bgnorm", "image", "a/b.zarr"), ("labels", "labels", "a/s.zarr")]
    for out in (rel.resolve("roi", placed, _Snap()),
                rel.resolve("roi", placed, _Snap({"a/s.zarr": ("a/b.zarr", "source_artifact")}))):
        for edge in out.edges:
            assert "evidence" in edge.to_dict()


def test_the_block_is_written_to_a_sidecar_as_well_as_to_attrs(tmp_path):
    class _Sdata:
        attrs: dict = {}

    out = rel.Relations("roi")
    out.add("derived_from", "bgnorm", "labels", "source_artifact")
    sdata = _Sdata()
    path = rel.write(out, str(tmp_path / "o.zarr"), sdata)

    assert path.exists(), "no sidecar was written"
    assert json.loads(path.read_text())["relations"][0]["source"] == "bgnorm"
    assert sdata.attrs[rel.ATTRS_KEY]["relations"][0]["source"] == "bgnorm"


def test_the_sidecar_is_read_back_without_opening_the_zarr(tmp_path):
    out = rel.Relations("roi")
    out.add("annotates", "table", "labels", "spatialdata_region")
    output = str(tmp_path / "o.zarr")
    rel.write(out, output, None)
    # No zarr was ever created; reading must still work.
    assert rel.read(output)["relations"][0]["kind"] == "annotates"


def test_a_dropped_attrs_does_not_lose_the_record(tmp_path):
    class _Hostile:
        """An sdata whose attrs silently refuse writes, as a validation pass would."""

        attrs: dict = {}

        def __setattr__(self, *_a):
            raise RuntimeError("attrs dropped")

    out = rel.Relations("roi")
    out.add("derived_from", "bgnorm", "labels", "source_artifact")
    output = str(tmp_path / "o.zarr")
    rel.write(out, output, _Hostile())
    assert rel.read(output)["relations"][0]["source"] == "bgnorm"


def test_the_block_is_namespaced_inside_attrs():
    assert rel.ATTRS_KEY == "clinicalomx"


def test_a_schema_version_travels_with_the_block():
    assert rel.Relations("roi").to_dict()["schema"] == rel.SCHEMA


# Tables annotating labels, which is SpatialData's own mechanism.
pytest.importorskip("spatialdata", reason="the builders need clinicalomx[extract]")


def _adata(columns):
    import anndata as ad
    import numpy as np
    import pandas as pd

    return ad.AnnData(obs=pd.DataFrame({c: np.arange(4) for c in columns}))


def test_a_table_with_an_instance_key_annotates_its_labels():
    from clinicalomx.extract.builders.spatialdata import _annotate

    table, why = _annotate(_adata(["cell_id"]), "labels_R0C0", "cell_id")
    assert why is None
    assert table.uns["spatialdata_attrs"]["region"] == "labels_R0C0"
    assert table.uns["spatialdata_attrs"]["instance_key"] == "cell_id"


def test_a_table_whose_instance_key_is_not_a_column_is_left_unannotated_and_says_why():
    from clinicalomx.extract.builders.spatialdata import _annotate

    table, why = _annotate(_adata(["area"]), "labels_R0C0", "cell_id")
    assert "spatialdata_attrs" not in table.uns
    assert "cell_id" in why and "not a column" in why


def test_a_table_with_no_labels_to_annotate_says_that_rather_than_failing():
    from clinicalomx.extract.builders.spatialdata import _annotate

    _table, why = _annotate(_adata(["cell_id"]), None, "cell_id")
    assert "no labels element" in why


def test_an_element_declaring_no_instance_key_is_left_unannotated():
    from clinicalomx.extract.builders.spatialdata import _annotate

    _table, why = _annotate(_adata(["cell_id"]), "labels_R0C0", None)
    assert "no instance_key" in why
