"""Data checks for the extract layer: the ROI records a recipe reads are fit to build from."""
from __future__ import annotations

import contextlib
import importlib

from clinicalomx.artifacts import newest_artifact
from clinicalomx.checks import Check, Snapshot
from clinicalomx.extract import ANY_CHILD, all_recipes
from clinicalomx.extract.builders.geo import read_frame, read_geometry
from clinicalomx.extract.builders.kinds import resolve_kind
from clinicalomx.extract.coords import EVIDENCE_TOLERANCE_PX, PlacementError
from clinicalomx.geometry import geometry_row, roi_id_for
from clinicalomx.names import FEATURES
from clinicalomx.vocab.registry import owners


def _declared_recipes():
    return all_recipes()


def roles_are_declared(snap: Snapshot) -> list[str]:
    """Every candidate role is declared as Artifact-dtype by some installed Vocabulary."""
    declared = set(snap.role_features())
    out = []
    for recipe in _declared_recipes():
        for element in recipe.elements:
            for role in element.roles:
                if role not in declared:
                    out.append(f"{recipe.name} [{element.name}] -> {role!r} is not declared by any "
                               f"installed Vocabulary as an Artifact-dtype feature")
    return out


def needs_features_are_declared(snap: Snapshot) -> list[str]:
    """Every ``needs_features`` name is a real feature on the instance."""
    import lamindb as ln

    live = {f.name for f in ln.Feature.filter()}
    out = []
    for recipe in _declared_recipes():
        for element in recipe.elements:
            for name in element.needs_features:
                if name not in live:
                    out.append(f"{recipe.name} [{element.name}] needs feature {name!r}, "
                               f"which does not exist on this instance")
    return out


MIN_POPULATION = 20


def needs_features_are_populated(snap: Snapshot) -> list[str]:
    """A ``needs_features`` name no record of its ROI subtype carries is a false declaration."""
    wanted: dict[str, set[str]] = {}
    for recipe in _declared_recipes():
        for element in recipe.elements:
            for roi_type in recipe.roi_types:
                targets = recipe.child_types if element.level == "children" else (roi_type,)
                for target in targets:
                    wanted.setdefault(target, set()).update(element.needs_features)

    live = set(snap.roi_types())
    out = []
    for roi_type, features in sorted(wanted.items()):
        if roi_type not in live:
            continue  # roi_types_exist owns that
        records = snap.records(roi_type)
        if len(records) < MIN_POPULATION:
            continue
        for feature in sorted(features):
            carried = sum(1 for r in records if snap.values(r).get(feature) is not None)
            if carried:
                continue
            owner = _owner_of(feature)
            out.append(
                f"{feature!r} is required by a recipe on every {roi_type}, but 0 of "
                f"{len(records)} {roi_type} records carry a value. This is a DECLARATION fault, "
                f"not a data gap. Owner: {owner}. Remove it from needs_features, or point the "
                f"element at a placement that does not read it.")
    return out


def _owner_of(feature: str) -> str:
    return owners().get(feature) or "(unowned - no installed vocabulary declares it)"


def roi_types_exist(snap: Snapshot) -> list[str]:
    """A recipe can only be asked for on an ROI subtype that exists."""
    live = set(snap.roi_types())
    return [f"{r.name} is declared for {t!r}, which is not an ROI subtype on this instance"
            for r in _declared_recipes() for t in r.roi_types if t not in live]


def child_types_exist(snap: Snapshot) -> list[str]:
    """Every child type a recipe expands into is an ROI subtype; else it plans zero units."""
    live = set(snap.roi_types())
    # ANY_CHILD is not a subtype and cannot be checked.
    return [f"{r.name} expands into {t!r}, which is not an ROI subtype on this instance"
            for r in _declared_recipes() for t in r.child_types
            if t != ANY_CHILD and t not in live]


def element_kind_matches_the_artifact(snap: Snapshot) -> list[str]:
    """A recipe's declared kind agrees with the actual artifact type/dtype."""
    out = []
    for recipe in _declared_recipes():
        for element in recipe.elements:
            for roi_type in recipe.roi_types:
                for rec in snap.records(roi_type):
                    for role in element.roles:
                        for key in snap.artifact_keys(rec, role):
                            actual = resolve_kind(key, variable=element.variable)
                            if actual is not None and actual != element.kind:
                                out.append(
                                    f"{recipe.name} [{element.name}] declares kind "
                                    f"{element.kind!r} but {key} is {actual!r}")
                                break
    return sorted(set(out))


def table_instance_key_exists(snap: Snapshot) -> list[str]:
    out = []
    for recipe in _declared_recipes():
        for element in recipe.elements:
            if element.kind != "table" or not element.instance_key:
                continue
            for roi_type in (*recipe.roi_types, *recipe.child_types):
                for rec in snap.records(roi_type):
                    for role in element.roles:
                        for key in snap.artifact_keys(rec, role):
                            columns = _table_columns(key)
                            if columns is not None and element.instance_key not in columns:
                                out.append(
                                    f"{recipe.name} [{element.name}] declares instance_key "
                                    f"{element.instance_key!r}, which is not a column of {key} "
                                    f"({sorted(columns)[:6]}). That table will be added "
                                    f"UNANNOTATED and will annotate no label image.")
                                break
    return sorted(set(out))


def _table_columns(key: str):
    """The obs columns of a table artifact, or ``None``."""

    with contextlib.suppress(Exception):
        art = newest_artifact([key])
        if art is None:
            return None
        path = str(art.path)
        if path.endswith(".h5ad"):
            import anndata as ad

            return set(ad.read_h5ad(path, backed="r").obs.columns)
        if path.endswith(".parquet"):
            import pyarrow.parquet as pq

            return set(pq.read_schema(path).names)
    return None


def placement_disagrees_with_evidence(snap: Snapshot) -> list[str]:
    """A metadata placement that contradicts the origin an image records for itself.

    One disagreement implicates every core on that slide. Reads zarr metadata, so it runs only
    under ``--include-storage``.
    """
    from clinicalomx.extract.builders.imaging import open_image

    out = []
    for recipe in _declared_recipes():
        placement_path = recipe.placement
        for roi_type in recipe.roi_types:
            for rec in snap.records(roi_type):
                values = snap.values(rec)
                for element in recipe.elements:
                    if not element.variable:
                        continue  # only a zarr variable records an origin
                    art = newest_artifact(
                        [k for role in element.roles for k in snap.artifact_keys(rec, role)])
                    if art is None:
                        continue
                    try:
                        origin = open_image(art, variable=element.variable).origin
                    except Exception:  # noqa: BLE001 - unreadable is no evidence, not a finding
                        continue
                    if origin is None:
                        continue
                    claimed = _metadata_bounds(
                        placement_path, rec, values,
                        read_frame, geometry_row, roi_id_for, FEATURES)
                    if not isinstance(claimed, tuple):
                        continue
                    off = max(abs(a - b)
                              for a, b in zip(claimed, origin.bounds, strict=True))
                    if off > EVIDENCE_TOLERANCE_PX:
                        out.append(
                            f"{rec.name} [{element.name}]: the image records an origin of "
                            f"({origin.x:.0f}, {origin.y:.0f}) but {placement_path} computes "
                            f"({claimed[0]:.0f}, {claimed[1]:.0f}) - off by {off:.0f}px. The "
                            f"image is a record of the crop that happened, so the metadata is "
                            f"wrong, and it is wrong for every ROI reading that same geometry.")
                    _ = PlacementError  # imported for the caller's benefit
    return out


def _metadata_bounds(placement_path, rec, values, read_frame, geometry_row, roi_id_for, FEATURES):
    """What the recipe's declared placement computes, or a string saying why it could not."""

    module, _, attr = placement_path.partition(":")
    try:
        placement = getattr(importlib.import_module(module), attr)()
    except Exception as exc:  # noqa: BLE001
        return f"placement unavailable: {exc}"

    parquet = newest_artifact(values.get(FEATURES.SOURCE_ARTIFACT)
                              or values.get(FEATURES.ROI_PARQUET))
    row = frame = None
    if parquet is not None:
        try:
            frame = read_frame(parquet.path)
            row = geometry_row(read_geometry(parquet.path),
                               roi_id=values.get(FEATURES.ROI_ID) or roi_id_for(str(rec.name)),
                               parquet_row=values.get("parquet_row"))
        except Exception as exc:  # noqa: BLE001
            return f"geometry unavailable: {exc}"
    try:
        return placement.bounds(values=values, geometry_row=row,
                                container_attrs={"frame": frame})
    except Exception as exc:  # noqa: BLE001
        return f"declined: {exc}"


def builders_are_resolvable(_snap: Snapshot) -> list[str]:
    """The ``builder`` and ``placement`` dotted paths import."""
    out = []
    for recipe in _declared_recipes():
        for label, dotted in (("builder", recipe.builder), ("placement", recipe.placement)):
            if not dotted:
                out.append(f"{recipe.name} declares no {label}")
                continue
            module, _, attr = dotted.partition(":")
            try:
                obj = importlib.import_module(module)
            except ImportError as exc:
                out.append(f"{recipe.name} {label} {dotted!r} does not import: {exc}")
                continue
            if attr and not hasattr(obj, attr):
                out.append(f"{recipe.name} {label} {dotted!r}: {module} has no {attr!r}")
    return out


def extract_checks() -> list[Check]:
    """Invariants over the recipe catalogue, framework and consumer alike."""
    return [
        Check("extract.roles_are_declared", "warn", roles_are_declared,
              "every candidate role is a declared Artifact-dtype feature somewhere"),
        Check("extract.needs_features_are_declared", "error", needs_features_are_declared,
              "a needs_features typo makes every ROI read as unplaceable"),
        Check("extract.needs_features_are_populated", "error", needs_features_are_populated,
              "a needs_features nothing carries is a false assertion, not a data gap"),
        Check("extract.roi_types_exist", "error", roi_types_exist,
              "a recipe is declared for an ROI subtype that exists"),
        Check("extract.child_types_exist", "error", child_types_exist,
              "a recipe expands into an ROI subtype that exists"),
        Check("extract.builders_are_resolvable", "warn", builders_are_resolvable,
              "the builder and placement dotted paths import in this environment"),
        Check("extract.placement_disagrees_with_evidence", "error",
              placement_disagrees_with_evidence,
              "a metadata strategy contradicts the origin an image records for itself",
              needs_storage=True),
        Check("extract.table_instance_key_exists", "warn", table_instance_key_exists,
              "a table's instance_key is a real column, so it can annotate its label image",
              needs_storage=True),
        Check("extract.element_kind_matches_the_artifact", "warn",
              element_kind_matches_the_artifact,
              "a declared element kind agrees with what the artifact actually is",
              needs_storage=True),
    ]
