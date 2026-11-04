"""ROI-centric semantic operations over lamindb.

Every ln.Artifact annotates one semantic ROI Record, for example:

    Image -- annotates --> SlideROI
    Crop bounding box -- annotates --> CoreROI
    Image crop -- annotates --> CoreROI

Links go both ways. The ROI Record has a role feature (such as ``segmentations``) listing its
Artifacts; the Artifact has ``roi_uid``, ``roi_name`` and ``roi_type`` naming its one ROI.

A transform declares what it does to ROIs:
  - :func:`update` - 1:1: the output annotates the same ROI as the input.
  - :func:`split` - 1:N: a parent ROI fans out to child ROIs (TMA to cores, image to tiles).
  - :func:`merge` - N:1: many ROIs collapse into one (cores to a merged dataset).
  - :func:`create` - annotate a fresh ROI the caller built.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from clinicalomx.features import upsert_values as _upsert

# Artifact -> ROI back-reference features.
ROI_UID = "roi_uid"      # str: uid of the ROI the artifact annotates (the pointer)
ROI_NAME = "roi_name"    # str: convenience mirror of the ROI's name
ROI_TYPE = "roi_type"    # str: convenience mirror of the ROI's type
# ROI -> ROI hierarchy (child -> parent).
PARENT_ROI = "parent_roi"


def ensure_features(role_features: dict | None = None) -> None:
    """Get-or-create the Features clinicalomx relies on.

    Always ensures ``roi_uid`` / ``roi_name`` / ``roi_type`` and ``parent_roi``.
    ``role_features`` maps extra ROI-side names to a dtype, or to ``(dtype, description)``.
    Prefer the description form: a used Feature cannot be deleted.
    """
    import lamindb as ln
    wanted: dict = {
        ROI_UID: (str, "uid of the ROI Record this artifact annotates."),
        ROI_NAME: (str, "Name of the ROI Record this artifact annotates."),
        ROI_TYPE: (str, "Type of the ROI Record this artifact annotates."),
        PARENT_ROI: (ln.Record, "The parent ROI this ROI was split from."),
    }
    wanted.update(role_features or {})
    for name, spec in wanted.items():
        dtype, description = spec if isinstance(spec, tuple) else (spec, None)
        if ln.Feature.filter(name=name).one_or_none() is None:
            kwargs = {"description": description} if description else {}
            ln.Feature(name=name, dtype=dtype, **kwargs).save()


def _already_linked(roi, artifact, feature: str) -> bool:
    """Is ``artifact`` already a value of ``roi.features[feature]``, as an object or as its key?"""
    current = roi.features.get_values().get(feature)
    if current is None:
        return False
    values = current if isinstance(current, (list, tuple, set, frozenset)) else [current]
    uid, key = getattr(artifact, "uid", None), getattr(artifact, "key", None)
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value in (uid, key):
                return True
        elif getattr(value, "uid", None) == uid or (
            key is not None and getattr(value, "key", None) == key
        ):
            return True
    return False


def annotate(roi, artifact, feature: str, *, ensure: bool = True):
    """Link ``artifact`` to ROI Record ``roi`` both ways. Idempotent.

    ``roi.features[feature]`` gains ``artifact``, and ``artifact`` gains the ``roi_uid`` /
    ``roi_name`` / ``roi_type`` back-refs. ``feature`` is the role, such as ``"segmentations"``.
    """
    import lamindb as ln
    if ensure:
        ensure_features({feature: ln.Artifact})
    if not _already_linked(roi, artifact, feature):
        roi.features.add_values({feature: artifact})  # multi-valued role: append
    back = {ROI_UID: roi.uid}
    if getattr(roi, "name", None) is not None:
        back[ROI_NAME] = str(roi.name)
    rtype = getattr(roi, "type", None)
    if rtype is not None:
        back[ROI_TYPE] = str(getattr(rtype, "name", rtype))
    # Back-refs are single-valued: per-key upsert.
    _upsert(artifact.features, back)
    return roi


def resolve_roi(
    artifact,
    *, 
    legacy_uid_features: Sequence[str] = (),
    legacy_name_features: Sequence[str] = ()
):
    """Return the ROI Record an ``artifact`` annotates, or ``None``.

    Reads ``roi_uid``, then the given legacy uid and name features in order.
    """
    try:
        vals = artifact.features.get_values()
    except Exception:
        vals = {}
    uid = vals.get(ROI_UID) or next((vals[f] for f in legacy_uid_features if vals.get(f)), None)
    if uid:
        import lamindb as ln

        roi = ln.Record.filter(uid=uid).one_or_none()
        if roi is not None:
            return roi
    name = vals.get(ROI_NAME) or next((vals[f] for f in legacy_name_features if vals.get(f)), None)
    if name:
        import lamindb as ln

        return ln.Record.filter(name=name).one_or_none()
    return None


def require_roi(artifact, **resolve_kw):
    """Like :func:`resolve_roi`, but raise if the artifact annotates no ROI."""
    roi = resolve_roi(artifact, **resolve_kw)
    if roi is None:
        raise ValueError(
            f"artifact {getattr(artifact, 'key', artifact)!r} annotates no ROI "
            f"(no {ROI_UID!r} back-ref or legacy fallback resolved)"
        )
    return roi


def update(input_art, output_art, feature: str, *, legacy_uid_features: Sequence[str] = (),
           legacy_name_features: Sequence[str] = (), strict: bool = False):
    """1:1 - ``output_art`` annotates the same ROI as ``input_art``.

    Returns the ROI Record, or ``None`` if the input annotates none (raises when ``strict``).
    """
    roi = resolve_roi(input_art, legacy_uid_features=legacy_uid_features,
                      legacy_name_features=legacy_name_features)
    if roi is None:
        if strict:
            raise ValueError(
                f"{getattr(input_art, 'key', input_art)!r} annotates no ROI to inherit"
            )
        return None
    return annotate(roi, output_art, feature)


def create(roi, artifact=None, feature: str | None = None, *, parent=None):
    """Wire a caller-built, saved ROI Record into the graph.

    Links it to ``parent`` when given, and annotates it with ``artifact`` under ``feature``.
    """
    if parent is not None:
        ensure_features()
        _upsert(roi.features, {PARENT_ROI: parent})  # one parent per child
    if artifact is not None and feature:
        annotate(roi, artifact, feature)
    return roi


def split(input_art, children: Iterable[tuple], feature: str, *, parent=None,
          legacy_uid_features: Sequence[str] = (), legacy_name_features: Sequence[str] = ()):
    """1:N - the input's ROI, or ``parent``, fans out to child ROIs.

    ``children`` is an iterable of ``(child_roi_record, artifact_or_None)``. Returns
    ``(parent_roi, [child_roi, ...])``.
    """
    parent = parent or require_roi(
        input_art, legacy_uid_features=legacy_uid_features,
        legacy_name_features=legacy_name_features,
    )
    children_out = []
    for child_roi, art in children:
        create(child_roi, art, feature, parent=parent)
        children_out.append(child_roi)
    return parent, children_out


def merge(input_arts, target_roi, feature: str, *, merged_artifact=None,
          legacy_uid_features: Sequence[str] = (), legacy_name_features: Sequence[str] = ()):
    """N:1 - many input ROIs merge into ``target_roi``.

    Returns ``(target_roi, [source_roi, ...])``.
    """
    ensure_features()
    source_rois = []
    for art in input_arts:
        r = resolve_roi(art, legacy_uid_features=legacy_uid_features,
                        legacy_name_features=legacy_name_features)
        if r is not None:
            source_rois.append(r)
            target_roi.features.add_values({PARENT_ROI: r})
    if merged_artifact is not None:
        annotate(target_roi, merged_artifact, feature)
    return target_roi, source_rois


# Typed ROI Record construction. The project owns the type names, descriptions and features.

def ensure_types(subtypes: dict | None = None, *, umbrella: str = "ROI",
                 umbrella_description: str | None = None):
    """Get-or-create the umbrella ROI type Record and its subtypes. Returns the umbrella.

    ``subtypes`` maps a subtype name to its description.
    """
    import lamindb as ln
    roi = ln.Record.filter(name=umbrella, is_type=True).one_or_none()
    if roi is None:
        roi = ln.Record(name=umbrella, is_type=True, description=umbrella_description).save()
    for name, desc in (subtypes or {}).items():
        if ln.Record.filter(name=name, is_type=True).one_or_none() is None:
            ln.Record(name=name, type=roi, is_type=True, description=desc).save()
    return roi


def get_type(name: str):
    """The ``is_type=True`` Record named ``name``. Raises if missing; call ensure_types."""
    import lamindb as ln
    t = ln.Record.filter(name=name, is_type=True).one_or_none()
    if t is None:
        raise LookupError(f"ROI type {name!r} not found; call clinicalomx.ensure_types() first")
    return t


def get_or_create(name: str, roi_type: str, *, features: dict | None = None):
    """Get-or-create the ROI Record ``name`` of subtype ``roi_type``, and stamp ``features``.

    ``None`` feature values are skipped.
    """
    import lamindb as ln
    t = get_type(roi_type)
    roi = ln.Record.filter(name=name, type=t).one_or_none()
    if roi is None:
        roi = ln.Record(name=name, type=t).save()
    clean = {k: v for k, v in (features or {}).items() if v is not None}
    _upsert(roi.features, clean)  # per-key upsert
    return roi