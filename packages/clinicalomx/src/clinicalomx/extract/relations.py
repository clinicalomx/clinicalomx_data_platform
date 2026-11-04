"""How the elements of one built object relate: which image a label image was segmented from, and
which label image a table annotates.

Derivation is read from the instance (``source_artifact``, then run lineage), never assumed; an
unknown source is ``None`` and reported. The block is written to ``sdata.attrs`` and to a
``.relations.json`` sidecar.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from clinicalomx.extract.keys import key_for
from clinicalomx.snapshot import type_name_of

#: Version of the block's shape.
SCHEMA = 1

#: Namespaced key for the block inside ``sdata.attrs``.
ATTRS_KEY = "clinicalomx"


@dataclass(frozen=True)
class Relation:
    """One directed edge between two elements of the same object."""

    #: ``derived_from`` - target's pixels were computed from source's.
    #: ``annotates`` - source (a table) describes the objects in target (a label image).
    kind: str
    #: The element key at the far end. ``None`` means nothing recorded it.
    source: str | None
    target: str
    #: How it was learned: ``source_artifact``, ``run_lineage``, ``spatialdata_region``, or
    #: ``None`` where the relation could not be established.
    evidence: str | None = None
    #: Elements that read the same lineage artifact, such as `bgnorm` and `bgnormQ`. The source
    #: stays ``None`` and these are listed.
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.source is not None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "source": self.source, "target": self.target,
                "evidence": self.evidence, "candidates": list(self.candidates)}


@dataclass
class Relations:
    """Every edge in one object, plus what could not be established."""

    anchor: str
    edges: list[Relation] = field(default_factory=list)

    def add(self, kind: str, source: str | None, target: str, evidence: str | None,
            candidates: tuple[str, ...] = ()) -> None:
        self.edges.append(Relation(kind, source, target, evidence, candidates))

    @property
    def unresolved(self) -> list[Relation]:
        return [e for e in self.edges if not e.resolved]

    def derived_from(self, target: str) -> str | None:
        return next((e.source for e in self.edges
                     if e.kind == "derived_from" and e.target == target), None)

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "anchor": self.anchor,
                "relations": [e.to_dict() for e in self.edges]}

    def caveats(self) -> list[str]:
        gaps = self.unresolved
        if not gaps:
            return []
        out = []
        unknown = [e for e in gaps if not e.candidates]
        ambiguous = [e for e in gaps if e.candidates]
        if unknown:
            names = ", ".join(sorted(e.target for e in unknown)[:5])
            out.append(
                f"{len(unknown)} element(s) are in this object with no recorded source image "
                f"({names}). They were NOT guessed - nothing on the instance says what they were "
                f"derived from, so the relation is null rather than plausible.")
        if ambiguous:
            first = ambiguous[0]
            out.append(
                f"{len(ambiguous)} element(s) have lineage to an artifact this object reads as "
                f"SEVERAL elements, so which one is not recoverable - e.g. {first.target} came "
                f"from the artifact behind {' or '.join(first.candidates)}. Naming one would be a "
                f"coin flip; the shortlist is recorded instead.")
        return out

    def summarise(self) -> str:
        if not self.edges:
            return "  (no relations)"
        lines = []
        for e in sorted(self.edges, key=lambda x: (x.kind, x.target)):
            arrow = "<-" if e.kind == "derived_from" else "->"
            source = e.source or (" or ".join(e.candidates) if e.candidates else "UNKNOWN")
            lines.append(f"  {e.target:34} {arrow} {source:34} {e.kind} ({e.evidence or '-'})")
        return "\n".join(lines)


def resolve(anchor: str, placed, snapshot) -> Relations:
    """The edges among the elements placed in one object.

    ``placed`` is ``[(element key, kind, artifact key)]``. Derivation is matched through the
    artifact to an image element in the same object.
    """
    rel = Relations(anchor)
    # One artifact key can map to several image elements.
    by_artifact: dict[str, list[str]] = {}
    for key, kind, art in placed:
        if kind == "image" and art:
            by_artifact.setdefault(art, []).append(key)

    labels = [(key, art) for key, kind, art in placed if kind == "labels"]
    if not labels:
        return rel

    provenance = snapshot.artifact_provenance([art for _key, art in labels if art])
    for key, art in labels:
        source_art, evidence = provenance.get(art, (None, None))
        if not source_art:
            rel.add("derived_from", None, key, None)
            continue
        options = by_artifact.get(source_art, [])
        if len(options) == 1:
            rel.add("derived_from", options[0], key, evidence)
        elif options:
            # Several elements read this artifact: record the shortlist, not a guess.
            rel.add("derived_from", None, key, f"{evidence}:ambiguous-variable",
                    tuple(sorted(options)))
        else:
            # Recorded, but pointing outside this object.
            rel.add("derived_from", None, key, f"{evidence}:outside-object")
    return rel


def planned_entries(unit, recipe, anchor_name: str, snapshot) -> list[tuple[str, str, str | None]]:
    """What the builder will place, as ``[(element key, kind, artifact key)]``. Reads nothing."""
    def newest(roi, role):
        keys = snapshot.artifact_keys(roi, role)
        return sorted(keys)[-1] if keys else None

    out: list[tuple[str, str, str | None]] = []
    anchor = snapshot.record_by_uid(unit.roi_uid)
    if anchor is None:
        return out

    for name, role in unit.elements.items():
        element = recipe.element(name)
        if element is None or element.level != "self":
            continue
        out.append((name, element.kind, newest(anchor, role)))

    taken: set = set()
    for child_name, roles in unit.child_elements.items():
        child = snapshot.record_by_any_name(child_name)
        if child is None:
            continue
        roi_type = type_name_of(child)
        for name, role in roles.items():
            element = recipe.element(name)
            if element is None or element.level != "children":
                continue
            key = key_for(anchor_name, child_name, roi_type, name, taken=taken)
            out.append((key, element.kind, newest(child, role)))
    return out


def sidecar_path(output: str) -> pathlib.Path:
    """``<output>.relations.json``, beside the object."""
    return pathlib.Path(str(output) + ".relations.json")


def write(relations: Relations, output: str, sdata=None) -> pathlib.Path:
    """Put the block in ``sdata.attrs`` and write the sidecar. Returns the sidecar path."""
    payload = relations.to_dict()
    if sdata is not None:
        try:
            sdata.attrs[ATTRS_KEY] = payload
        except Exception:  # noqa: BLE001 - the sidecar is the durable copy; attrs is convenience
            pass
    path = sidecar_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return path


def read(output: str) -> dict | None:
    """The block for a built object, from the sidecar first, else from ``attrs``."""
    path = sidecar_path(output)
    if path.exists():
        return json.loads(path.read_text())
    try:
        from spatialdata import read_zarr

        return read_zarr(output).attrs.get(ATTRS_KEY)
    except Exception:  # noqa: BLE001
        return None
