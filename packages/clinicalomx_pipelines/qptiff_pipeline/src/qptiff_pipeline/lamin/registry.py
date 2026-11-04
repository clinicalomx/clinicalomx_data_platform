"""Idempotent get-or-create for the lamindb objects the stages register.

Re-running a stage skips anything already registered.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def get_or_create_artifact(local_path, key, *, description=None, write: Callable[[], Any] | None = None):
    """Return the Artifact with ``key``, or write and register it.

    ``write`` is a thunk, so the file is only written when the key is new.
    """
    import lamindb as ln

    existing = ln.Artifact.filter(key=key).first()
    if existing is not None:
        return existing
    if write is not None:
        write()
    return ln.Artifact(local_path, key=key, description=description).save()


def get_or_create_record(name, type, feature_values: dict | None = None):
    """Return the existing Record by (name, type); else create and annotate once."""
    import lamindb as ln

    from qptiff_pipeline.lamin.linking import link_artifact_entity

    existing = ln.Record.filter(name=name, type=type).first()
    if existing is not None:
        return existing
    rec = ln.Record(name=name, type=type).save()
    if feature_values:
        rec.features.add_values(feature_values)
        # Also show Artifact-valued features as hub linked entities.
        for fname, val in feature_values.items():
            if isinstance(val, ln.Artifact):
                link_artifact_entity(rec, val, fname)
    return rec


def get_or_create_type(name: str):
    """Get-or-create a Record *type* (``is_type=True``)."""
    import lamindb as ln

    return (
        ln.Record.filter(name=name, is_type=True).one_or_none()
        or ln.Record(name=name, is_type=True).save()
    )


def get_or_create_project(name: str, description: str | None = None):
    """Get-or-create the pipeline's ln.Project, filling in a missing description."""
    import lamindb as ln

    project = ln.Project.filter(name=name).one_or_none()
    if project is None:
        return ln.Project(name=name, description=description).save()
    if description and not project.description:
        project.description = description
        project.save()
    return project

