"""SpatialData builders, all in one coordinate system.

``build_core`` writes one Core. ``build_merged`` writes one object per parent ROI with an element
per child, each placed in the ``global`` space by a ``Translation``. ``build_slide`` writes the
whole-slide pyramid plus its shapes.
"""
from __future__ import annotations

import pathlib
import shutil

from clinicalomx.artifacts import newest_artifact
from clinicalomx.extract import relations as rel
from clinicalomx.extract.builders.geo import read_geometry
from clinicalomx.extract.keys import key_for
from clinicalomx.snapshot import type_name_of


def _elements_for(unit, recipe, placement, snapshot):
    """``{element name: (element, artifact, bounds_or_None)}`` for one unit."""
    from clinicalomx.extract.builders.ometiff import _artifact_for, _resolve

    roi, values, row, frame = _resolve(unit, snapshot)

    out = {}
    for name, role in unit.elements.items():
        element = recipe.element(name)
        art = _artifact_for(roi, role, values, snapshot)
        if art is None:
            continue
        bounds = None
        if element is not None and element.needs_features:
            bounds = placement.bounds(values=values, geometry_row=row,
                                      container_attrs={"frame": frame})
        out[name] = (element, art, bounds)
    return out


def _image_element(art, bounds, space: str, *, variable=None):
    """An Image2DModel in ``space``, cropped lazily to ``bounds`` and translated back into place."""
    from spatialdata.models import Image2DModel
    from spatialdata.transformations import Identity, Translation

    from clinicalomx.extract.builders.imaging import as_cyx, crop, open_image

    opened = open_image(art, variable=variable)
    if bounds is None:
        image = as_cyx(opened.array.compute() if hasattr(opened.array, "compute")
                       else opened.array)
        return Image2DModel.parse(image, dims=("c", "y", "x"),
                                  transformations={space: Identity()})

    cropped, (xa, ya, _xb, _yb) = crop(opened, bounds)
    return Image2DModel.parse(
        cropped, dims=("c", "y", "x"),
        transformations={space: Translation([xa, ya], axes=("x", "y"))})


def _shapes_element(art, space: str):
    from spatialdata.models import ShapesModel
    from spatialdata.transformations import Identity


    return ShapesModel.parse(read_geometry(art.path), transformations={space: Identity()})


def _annotate(adata, region: str | None, instance_key: str | None):
    """Make the table annotate a label image through ``region`` and ``instance_key``.

    A missing or misnamed ``instance_key`` column leaves the table unannotated and returns why.
    """
    from spatialdata.models import TableModel

    if region is None:
        return adata, "no labels element in this object to annotate"
    if not instance_key:
        return adata, "the element declares no instance_key"
    if instance_key not in adata.obs.columns:
        return adata, (f"instance_key {instance_key!r} is not a column of this table "
                       f"({list(adata.obs.columns)[:6]})")

    import pandas as pd

    region_key = "region"
    adata.obs[region_key] = pd.Categorical([region] * adata.n_obs)
    return TableModel.parse(adata, region=region, region_key=region_key,
                            instance_key=instance_key), None


def _table_element(art):
    """A SpatialData table (AnnData) from an h5ad or a parquet artifact."""
    import anndata as ad

    path = str(art.path)
    if path.endswith(".h5ad"):
        return ad.read_h5ad(path)
    if path.endswith(".parquet"):
        import pandas as pd

        return ad.AnnData(obs=pd.read_parquet(path))
    if path.endswith(".csv"):
        import pandas as pd

        return ad.AnnData(obs=pd.read_csv(path))
    raise ValueError(
        f"{path}: no table reader for this suffix. A SpatialData table is an AnnData; h5ad, "
        f"parquet and csv are the shapes this layer knows how to make one from.")


def _labels_element(art, space: str, *, variable=None):
    import numpy as np
    from spatialdata.models import Labels2DModel
    from spatialdata.transformations import Identity

    from clinicalomx.extract.builders.imaging import open_image

    opened = open_image(art, variable=variable)
    labels = np.asarray(opened.array.compute() if hasattr(opened.array, "compute")
                        else opened.array)
    if labels.ndim == 3 and labels.shape[0] == 1:
        labels = labels[0]
    return Labels2DModel.parse(labels, dims=("y", "x"), transformations={space: Identity()})


def _add(sdata, kind: str, key: str, art, bounds, space: str, *, variable=None,
         region: str | None = None, instance_key: str | None = None,
         notes: list | None = None) -> None:
    if kind == "image":
        sdata.images[key] = _image_element(art, bounds, space, variable=variable)
    elif kind == "labels":
        sdata.labels[key] = _labels_element(art, space, variable=variable)
    elif kind == "shapes":
        sdata.shapes[key] = _shapes_element(art, space)
    elif kind == "table":
        table, why = _annotate(_table_element(art), region, instance_key)
        sdata.tables[key] = table
        if why is not None and notes is not None:
            notes.append(f"{key} annotates nothing ({why})")
    else:
        raise ValueError(f"no SpatialData slot for element kind {kind!r}")


def _write(sdata, output: str) -> None:
    out = pathlib.Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    sdata.write(out)


#: Element write order. Tables go last, after the label images they annotate.
_ORDER = {"image": 0, "labels": 1, "shapes": 2, "points": 3, "table": 4}


def _placement_order(items):
    return sorted(items, key=lambda kv: _ORDER.get(kv[1][0], 9))


def build_core(unit, *, recipe, placement, snapshot, **_kw) -> str:
    """One Core as a SpatialData: image, plus labels and a table where they exist."""
    from spatialdata import SpatialData


    space = recipe.coordinate_space
    sdata = SpatialData()
    found = _elements_for(unit, recipe, placement, snapshot)
    entries = {name: ((element.kind if element else "image"), element, art, bounds)
               for name, (element, art, bounds) in found.items()}

    placed, notes = [], []
    labels_key = next((k for k, (kind, *_r) in entries.items() if kind == "labels"), None)
    for name, (kind, element, art, bounds) in _placement_order(entries.items()):
        _add(sdata, kind, name, art, bounds, space,
             variable=element.variable if element else None,
             region=labels_key if kind == "table" else None,
             instance_key=element.instance_key if element else None, notes=notes)
        placed.append((name, kind, getattr(art, "key", None)))

    relations = rel.resolve(unit.roi_name, placed, snapshot)
    for entry in placed:
        if entry[1] == "table" and labels_key and not notes:
            relations.add("annotates", entry[0], labels_key, "spatialdata_region")
    _write(sdata, unit.output)
    rel.write(relations, unit.output, sdata)

    detail = (f"{len(sdata.images)} image(s), {len(sdata.labels)} label(s), "
              f"{len(sdata.tables)} table(s)")
    return detail + _relation_detail(relations, notes)


def _relation_detail(relations, notes) -> str:
    """The relations clause of the build detail, naming every gap."""
    out = ""
    resolved = [e for e in relations.edges if e.resolved]
    if resolved:
        out += f"; {len(resolved)} relation(s) recorded"
    gaps = relations.unresolved
    if gaps:
        out += (f"; {len(gaps)} element(s) with NO recorded source image "
                f"({', '.join(sorted(e.target for e in gaps)[:3])})")
    if notes:
        out += f"; {'; '.join(notes[:3])}"
    return out


def build_slide(unit, *, recipe, placement, snapshot, **_kw) -> str:
    """A whole slide: the pyramid, plus whatever shapes were detected on it."""
    return build_core(unit, recipe=recipe, placement=placement, snapshot=snapshot)


def build_merged(unit, *, recipe, placement, snapshot, **_kw) -> str:
    """One SpatialData for a parent ROI, with an element per child, in one coordinate space.

    The unit is the parent; children are walked from it.
    """
    from spatialdata import SpatialData


    space = recipe.coordinate_space
    parent = snapshot.record_by_uid(unit.roi_uid)
    if parent is None:
        raise LookupError(f"{unit.roi_name}: the ROI Record has gone since the plan was made")

    sdata = SpatialData()
    # Parent-level elements first.
    for name, role in unit.elements.items():
        element = recipe.element(name)
        if element is None or element.level != "self":
            continue
        art = newest_artifact(snapshot.artifact_keys(parent, role))
        if art is not None:
            _add(sdata, element.kind, name, art, None, space, variable=element.variable)

    skipped, taken = [], set()
    placed, notes, table_edges = [], [], []
    for child in recipe.targets_under(parent, snapshot):
        name, roi_type = str(child.name), type_name_of(child)
        # A child whose subtype has no declared placement is skipped and named.
        placer = placement.for_roi(roi_type) if hasattr(placement, "for_roi") else placement
        if placer is None:
            skipped.append(f"{name} ({roi_type}: no placement declared for this subtype)")
            continue
        # This child's own roles.
        stub = _ChildUnit(child.uid, name, unit.child_elements.get(name, unit.elements))
        try:
            found = _elements_for(stub, recipe, placer, snapshot)
        except Exception as exc:  # noqa: BLE001 - one bad core must not lose the TMA
            skipped.append(f"{name} ({type(exc).__name__})")
            continue
        # Resolve every key for the child first, so a table can name the same child's labels key.
        entries = {}
        for label, (element, art, bounds) in found.items():
            if element is None or element.level != "children":
                continue
            entries[label] = (element.kind, element, art, bounds,
                              key_for(unit.roi_name, name, roi_type, label, taken=taken))
        child_labels = next((v[4] for v in entries.values() if v[0] == "labels"), None)

        for label, (kind, element, art, bounds, key) in _placement_order(entries.items()):
            try:
                _add(sdata, kind, key, art, bounds, space, variable=element.variable,
                     region=child_labels if kind == "table" else None,
                     instance_key=element.instance_key, notes=notes)
                placed.append((key, kind, getattr(art, "key", None)))
                if kind == "table" and child_labels:
                    table_edges.append((key, child_labels))
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{name}/{label} ({type(exc).__name__}: {exc})")

    relations = rel.resolve(unit.roi_name, placed, snapshot)
    for table_key, labels_key in table_edges:
        relations.add("annotates", table_key, labels_key, "spatialdata_region")

    _write(sdata, unit.output)
    rel.write(relations, unit.output, sdata)
    detail = (f"{len(sdata.images)} image(s), {len(sdata.labels)} label(s), "
              f"{len(sdata.shapes)} shape(s), {len(sdata.tables)} table(s) in {space!r}")
    detail += _relation_detail(relations, notes)
    return detail + (f"; SKIPPED {', '.join(skipped)}" if skipped else "")


class _ChildUnit:
    """Adapts a child Record to the Unit shape ``_elements_for`` reads."""

    def __init__(self, uid, name, elements):
        self.roi_uid, self.roi_name, self.elements = uid, name, elements
