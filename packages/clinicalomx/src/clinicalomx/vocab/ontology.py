"""Pinned public-ontology imports, shared by ``steps/s0004_bionty`` and
:meth:`clinicalomx.vocab.Vocabulary.source`.

Never float to ``latest-version``: bionty resolves a different parquet per version.
"""

from __future__ import annotations

from typing import Any


def pinned_source(entity: str, spec: dict[str, Any]):
    """The exact ``bt.Source`` row a pin names. Raises if this bionty install lacks it."""
    import bionty as bt

    # The lockfile's `source` is `name` on bt.Source.
    query = {
        "entity": f"bionty.{entity}",
        "name": spec["source"],
        "version": spec["version"],
    }
    if spec.get("organism"):
        query["organism"] = spec["organism"]
    return bt.Source.get(**query)


def already_imported(registry, source) -> bool:
    """True if any record already carries this source. ``import_source`` is expensive."""
    return registry.filter(source=source).exists()


def reconcile(rec, entity: str, spec: dict[str, Any]):
    """Import ``entity`` at its pinned version if it is not already present. Idempotent.

    Records already held at another release stay there, unless the pin declares ``relink``: then
    all of them move onto the pin, or none do and the blocking record is named.
    """
    import bionty as bt

    registry = getattr(bt, entity)
    source = pinned_source(entity, spec)
    label = f"{entity}@{spec['version']}"

    stale = _held_elsewhere(registry, source) if spec.get("relink") else []
    if stale:
        moved = ", ".join(f"{oid} from {version}" for oid, version in stale)
        rec._log("bionty", label, "repair", f"relink {len(stale)} record(s): {moved}")
        if not rec.dry_run:
            _relink(registry, source)
        return source

    if already_imported(registry, source):
        rec._log("bionty", label, "noop")
        return source

    rec._log("bionty", label, "create", f"{spec['source']} / {spec.get('organism', '-')}")
    if not rec.dry_run:
        registry.import_source(source)
    return source


def _held_elsewhere(registry, source) -> list[tuple[str, str]]:
    """``(ontology id, version)`` of every record of this ontology held at another release."""
    query = {"source__entity": source.entity, "source__name": source.name}
    if hasattr(registry, "organism_id"):
        query["organism__name"] = source.organism
    return sorted(registry.filter(**query).exclude(source=source)
                  .values_list("ontology_id", "source__version"), key=str)


def _relink(registry, source) -> None:
    """Move every record held at another release onto ``source``, or raise and move none."""
    from django.db import transaction

    with transaction.atomic():
        registry.import_source(source, update_records=True)
        left = _held_elsewhere(registry, source)
        if left:
            at_pin = set(registry.public(source=source).to_dataframe().index)
            rows = "; ".join(
                f"{oid} at {version} ("
                + ("renamed at the pin and linked to artifacts" if oid in at_pin
                   else "absent from the pinned release") + ")"
                for oid, version in left)
            raise ValueError(
                f"{registry.__name__}: {len(left)} record(s) cannot be relinked to "
                f"{source.name} {source.version}, so none were: {rows}")
        registry.import_source(source)


def validate_pin(entity: str, version: str) -> None:
    """Reject ``latest`` or an empty version at declaration time."""
    if not version or str(version) in {"latest-version", "latest"}:
        raise ValueError(
            f"{entity}: ontology version must be pinned, got {version!r}. A floating "
            "version changes how free-text names resolve without any diff to review."
        )
