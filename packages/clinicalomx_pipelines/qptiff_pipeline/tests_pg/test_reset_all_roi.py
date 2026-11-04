"""``reset-all-roi`` deletes what its Storage scope reaches and nothing else.

Runs the real flow on a real lamindb holding a pipeline Storage and a croi Storage, with collections
and record links on both sides.
"""
from __future__ import annotations

import lamindb as ln
import pytest
from lamindb.models import CollectionArtifact, RecordArtifact, RecordRecord

from conftest import WORK
from qptiff_pipeline.domain import states
from qptiff_pipeline.flows.reset_all_roi import reset_all_roi
from qptiff_pipeline.lamin.registry import get_or_create_type

CONFIRM = "wipe-clinicalomx"


def _artifact(tmp_path, key: str) -> ln.Artifact:
    path = tmp_path / key.replace("/", "_")
    path.write_text(key)
    return ln.Artifact(path, key=key).save()


@pytest.fixture
def world(tmp_path):
    default_root = ln.setup.settings.storage.root_as_str
    dbstorage = WORK / f"dbstorage-{tmp_path.name}"
    dbstorage.mkdir()
    ln.Storage(root=str(dbstorage)).save()

    ln.settings.storage = str(dbstorage)
    try:
        owned_a = _artifact(tmp_path, "clinicalomx-qptiff-pipeline/s1/accepted.parquet")
        owned_b = _artifact(tmp_path, "clinicalomx-qptiff-pipeline/s2/accepted.parquet")
        shared_pipeline = _artifact(tmp_path, "clinicalomx-qptiff-pipeline/s3/accepted.parquet")
    finally:
        ln.settings.storage = default_root
    croi_a = _artifact(tmp_path, "core_labels/a.json")
    croi_b = _artifact(tmp_path, "shared/images/b.png")

    feedback = ln.Collection([owned_a, owned_b], key="clinical-roi/core/feedback").save()
    annotated = ln.Collection([croi_a, croi_b], key="clinical-roi/core/annotated_images").save()
    shared = ln.Collection([shared_pipeline, croi_a], key="mixed").save()

    image = ln.Feature(name=f"image_{tmp_path.name}", dtype=ln.Artifact).save()
    core = ln.Record(name="core-1", type=get_or_create_type(states.CORE_TYPE)).save()
    core.features.add_values({image.name: owned_a})

    specimen_type = ln.Record(name=f"Specimen_{tmp_path.name}", is_type=True).save()
    parent = ln.Record(name="specimen-parent", type=specimen_type).save()
    child = ln.Record(name="specimen-child", type=specimen_type).save()
    parent_feature = ln.Feature(name=f"parent_{tmp_path.name}", dtype=specimen_type).save()
    child.features.add_values({image.name: croi_a, parent_feature.name: parent})

    yield {
        "dbstorage": str(dbstorage), "core": core, "child": child,
        "owned": (owned_a, owned_b), "shared_pipeline": shared_pipeline, "croi": (croi_a, croi_b),
        "feedback": feedback, "annotated": annotated, "shared": shared,
    }

    # Clean up: the instance is shared by the session.
    records = [core, child, parent]
    for model in (RecordArtifact, RecordRecord):
        qs = model.objects.filter(record__in=records)
        qs._raw_delete(qs.db)
    for c in ln.Collection.objects.filter(id__in=[feedback.id, annotated.id, shared.id]):
        c.delete(permanent=True)
    arts = [owned_a, owned_b, shared_pipeline, croi_a, croi_b]
    for a in ln.Artifact.objects.filter(id__in=[a.id for a in arts]):
        a.delete(permanent=True, storage=True)
    for r in ln.Record.objects.filter(id__in=[r.id for r in records]):
        r.delete(permanent=True)


def _exists(model, obj) -> bool:
    return model.objects.filter(id=obj.id).exists()


def test_a_collection_outside_the_storage_scope_survives(world):
    result = reset_all_roi(dry_run=False, confirm=CONFIRM, storage_root=world["dbstorage"])

    assert result["collections_deleted"] == 1
    assert not _exists(ln.Collection, world["feedback"])

    # A croi collection, whose artifacts live in another Storage, survives.
    annotated = ln.Collection.objects.get(id=world["annotated"].id)
    assert annotated.branch_id == 1, "moved to the trash is still reached"
    assert {a.id for a in annotated.artifacts.all()} == {a.id for a in world["croi"]}
    for a in world["croi"]:
        assert _exists(ln.Artifact, a)


def test_a_collection_sharing_a_member_with_the_scope_survives_whole(world):
    reset_all_roi(dry_run=False, confirm=CONFIRM, storage_root=world["dbstorage"])

    shared = ln.Collection.objects.get(id=world["shared"].id)
    assert shared.branch_id == 1
    # Its pipeline member stays too.
    assert _exists(ln.Artifact, world["shared_pipeline"])
    assert CollectionArtifact.objects.filter(collection_id=shared.id).count() == 2


def test_record_links_outside_the_scope_survive(world):
    reset_all_roi(dry_run=False, confirm=CONFIRM, storage_root=world["dbstorage"])

    assert not _exists(ln.Record, world["core"])
    for a in world["owned"]:
        assert not _exists(ln.Artifact, a)
    # A non-ROI record's links, to a croi artifact and to another non-ROI record, are untouched.
    assert RecordArtifact.objects.filter(record_id=world["child"].id).count() == 1
    assert RecordRecord.objects.filter(record_id=world["child"].id).count() == 1


def test_a_dry_run_counts_the_scope_and_deletes_nothing(world):
    result = reset_all_roi(dry_run=True, storage_root=world["dbstorage"])

    assert result["collections"] == 1
    assert result["collections_shared"] == 1
    assert result["dbstorage_artifacts"] == 3
    assert result["cores"] == 1
    for key in ("feedback", "annotated", "shared", "core"):
        assert _exists(type(world[key]), world[key])
    for a in (*world["owned"], world["shared_pipeline"], *world["croi"]):
        assert _exists(ln.Artifact, a)
