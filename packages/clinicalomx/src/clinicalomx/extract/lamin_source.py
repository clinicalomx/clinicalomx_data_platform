"""The lamindb implementation of :class:`~clinicalomx.extract.source.Source`.

``filter_with_features`` inspects only the first keyword, so a call mixing a field and a feature
raises. Use one predicate per ``filter()``, chained, then ``.distinct()``. Link-table joins
(``values_json``, ``values_record``, ``values_artifact``) are field-only and always safe.
``scripts/probe_extract_assumptions.py`` checks this against a live instance.

``import lamindb`` connects, so it stays inside the methods.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clinicalomx.extract.source import Artifact, Selection
from clinicalomx.names import FEATURES


class LamindbSource:
    """Entities and artifacts read from the connected lamindb instance."""

    def __init__(self) -> None:
        self._slug: str | None = None

    def slug(self) -> str:
        """The instance slug, resolved once."""
        if self._slug is None:
            try:
                import lamindb as ln

                self._slug = ln.setup.settings.instance.slug
            except Exception:  # noqa: BLE001 - provenance must never be what fails a call
                self._slug = "(not connected)"
        return self._slug

    def entities(self, selection: Selection) -> list[Any]:
        """ROI Records narrowed in SQL: one chained ``filter()`` per predicate, then ``distinct()``.

        ``has_roles`` proves a link exists, not that an Artifact or its bytes do.
        """
        import lamindb as ln


        qs = (ln.Record.filter(type__name=selection.roi_type) if selection.roi_type
              else ln.Record.filter(is_type=False))
        if selection.name_contains:
            qs = qs.filter(name__icontains=selection.name_contains)
        if selection.parent_uid:
            qs = qs.filter(values_record__feature__name=FEATURES.PARENT_ROI,
                           values_record__value__uid=selection.parent_uid)
        if selection.state:
            qs = qs.filter(values_json__feature__name=FEATURES.PIPELINE_STATE,
                           values_json__value=selection.state)
        for role in selection.has_roles:
            # The join form: field-only, so it cannot raise on an unknown label.
            qs = qs.filter(values_artifact__feature__name=role)
        # Required: joins over multi-valued features return duplicates.
        return list(qs.distinct()[:selection.limit])

    def entity(self, *, uid: str | None = None, name: str | None = None,
               is_type: bool | None = None, order_by: str | None = None) -> Any | None:
        import lamindb as ln

        if uid is None and name is None:
            raise ValueError("entity() needs a uid or a name")
        if uid is not None:
            # A uid is unique; two matches is an error.
            return ln.Record.filter(uid=uid).one_or_none()
        predicate: dict[str, Any] = {"name": name}
        if is_type is not None:
            predicate["is_type"] = is_type
        # Both are fields, so one call is safe.
        qs = ln.Record.filter(**predicate)
        return (qs.order_by(order_by) if order_by else qs).first()

    def artifacts(self, artifact_ids: Iterable[str]) -> list[Artifact]:
        """Rows for the given storage keys, newest registration per key, with bytes probed.

        One query per key. The entity fields are left unset.
        """
        import lamindb as ln

        out: list[Artifact] = []
        for key in artifact_ids:
            art = ln.Artifact.filter(key=key).order_by("-created_at").first()
            if art is not None:
                out.append(_row(key, art))
        return out


def _row(key: str, art) -> Artifact:
    """One artifact row. An unreachable storage backend reads as ``stored=False``.

    An empty ``suffix`` (lamindb records ``.qptiff`` as ``""``) becomes ``None``.
    """
    suffix = art.suffix or None
    try:
        path = art.path
        return Artifact(artifact_id=key, uri=str(path), format=suffix, stored=path.exists())
    except Exception:  # noqa: BLE001 - "not verified present" is a state, not a failure
        return Artifact(artifact_id=key, format=suffix)
