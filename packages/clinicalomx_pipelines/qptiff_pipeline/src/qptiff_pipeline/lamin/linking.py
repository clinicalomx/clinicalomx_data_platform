"""Linking an Artifact to a Record, and an Artifact to its run's inputs.

A feature value is what ``get_values()`` reads. A linked entity (the ArtifactRecord M2M) is what
LaminHub shows as a clickable relation. :func:`set_artifact_feature` writes both.
"""
from __future__ import annotations


def link_artifact_entity(record, artifact, feature_name: str | None = None) -> None:
    """Link an Artifact to a Record as a hub linked entity, with its Feature as the role.
    Idempotent.

    Adds to the feature value; it does not replace it.
    """
    import lamindb as ln
    from lamindb.models import ArtifactRecord

    if artifact is None or record is None:
        return
    feat = ln.Feature.filter(name=feature_name).one_or_none() if feature_name else None
    if ArtifactRecord.filter(artifact=artifact, record=record, feature=feat).exists():
        return
    ArtifactRecord(artifact=artifact, record=record, feature=feat).save()


def set_artifact_feature(record, name: str, artifact) -> None:
    """Attach an Artifact to a record as a single-valued feature and as a hub linked entity."""
    if artifact is None:
        return
    if record.features.get_values().get(name) is not None:
        record.features.remove_values(name)
    record.features.add_values({name: artifact})
    link_artifact_entity(record, artifact, name)


def link_input(saved_artifact, *parents) -> None:
    """Attach upstream Artifacts as inputs of the run that produced ``saved_artifact``.

    Uses ``saved_artifact.run``, never ``ln.context.run``: lineage must land on the artifact's run.
    """
    if saved_artifact.run is None:
        return
    for p in parents:
        if p is not None:
            saved_artifact.run.input_artifacts.add(p)

