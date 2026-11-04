"""Checks for the ROI Record types."""
from __future__ import annotations

from clinicalomx.checks import Check, Snapshot
from clinicalomx.names import ALL_FEATURES, ALL_TYPES, FEATURES

# 'WholeSlide' is the top-level ROI.
ROOT_ROI_TYPES = ("WholeSlide",)


def _child_roi_types(snap: Snapshot) -> list[str]:
    return [t for t in snap.roi_types() if t not in ROOT_ROI_TYPES]


def artifacts_are_anchored(snap: Snapshot) -> list[str]:
    """Every artifact on an ROI role carries the back-refs ``annotate`` promises."""
    backrefs = snap.artifact_backrefs()
    out = []
    for rec, feature, key in snap.role_artifacts():
        refs = backrefs.get(key)
        if refs is None:
            out.append(f"{rec.name} [{feature}] -> {key} has no registered Artifact")
        elif refs.get(FEATURES.ROI_UID) is None:
            out.append(f"{rec.name} [{feature}] -> {key} has no roi_uid")
    return out


def one_roi_per_artifact(snap: Snapshot) -> list[str]:
    """The back-refs are single-valued, so an artifact must not claim two ROIs."""
    backrefs = snap.artifact_backrefs()
    seen: dict[str, str] = {}
    out = []
    for rec, feature, key in snap.role_artifacts():
        anchored_to = (backrefs.get(key) or {}).get(FEATURES.ROI_NAME)
        if anchored_to is None:
            continue  # covered by artifacts_are_anchored
        prior = seen.setdefault(key, anchored_to)
        if prior != anchored_to:
            out.append(f"{key} anchored to {prior!r} but also reached via {rec.name} [{feature}]")
    return out


def children_have_a_parent(snap: Snapshot) -> list[str]:
    """``split`` sets ``parent_roi``, so every non-root ROI must have one."""
    out = []
    for type_name in _child_roi_types(snap):
        for rec in snap.records(type_name):
            if snap.values(rec).get(FEATURES.PARENT_ROI) is None:
                out.append(f"{rec.name} ({type_name}) has no parent_roi")
    return out


def parent_matches_the_name_spine(snap: Snapshot) -> list[str]:
    """``parent_roi`` agrees with the ``<a>:<b>:<c>`` name spine."""
    out = []
    for type_name in _child_roi_types(snap):
        for rec in snap.records(type_name):
            parent = snap.values(rec).get(FEATURES.PARENT_ROI)
            if parent is None:
                continue  # covered by children_have_a_parent
            # cat[Record] values come back as the record name string.
            expected = rec.name.rsplit(":", 1)[0]
            actual = getattr(parent, "name", parent)
            if str(actual) != expected:
                out.append(f"{rec.name}: parent_roi={actual!r} but the name implies {expected!r}")
    return out


def roi_names_are_unique(snap: Snapshot) -> list[str]:
    """Two Records of one type must not share a name."""
    out = []
    for type_name in snap.roi_types():
        counts: dict[str, int] = {}
        for rec in snap.records(type_name):
            counts[rec.name] = counts.get(rec.name, 0) + 1
        out += [f"{type_name} {name!r} appears {n} times" for name, n in counts.items() if n > 1]
    return out


def _patient_features() -> tuple[str, ...]:
    """The two foundation features that carry a patient.

    ``patient`` (``type:Patient``) round-trips as the Patient Record's name; ``patient_id`` is the
    deidentified string.
    """
    return (FEATURES.PATIENT, FEATURES.PATIENT_ID)


def _sole_patient(values: dict, feature: str) -> str | None:
    """The single patient value under ``feature``, or None. A multi-valued cell reads as None."""
    v = values.get(feature)
    if v is None or isinstance(v, (list, tuple, set, frozenset)):
        return None
    return str(getattr(v, "name", v))


def one_patient_per_roi(snap: Snapshot) -> list[str]:
    """An ROI carries at most ONE patient."""
    features = _patient_features()
    out = []
    for type_name in snap.roi_types():
        for rec in snap.records(type_name):
            values = snap.values(rec)
            for feature in features:
                v = values.get(feature)
                if isinstance(v, (list, tuple, set, frozenset)) and len(v) > 1:
                    held = sorted(str(getattr(x, "name", x)) for x in v)
                    out.append(f"{rec.name} carries {len(v)} {feature} values: {held}")
    return out


def patient_agrees_with_the_parent(snap: Snapshot) -> list[str]:
    """Every nested or child ROI has the same patient as its parent."""
    features = _patient_features()
    out = []
    for type_name in snap.roi_types():
        for rec in snap.records(type_name):
            parent = snap.parent(rec)
            if parent is None:
                continue
            child_values, parent_values = snap.values(rec), snap.values(parent)
            for feature in features:
                mine = _sole_patient(child_values, feature)
                theirs = _sole_patient(parent_values, feature)
                if mine is not None and theirs is not None and mine != theirs:
                    out.append(
                        f"{rec.name}: {feature}={mine!r} but its parent {parent.name} "
                        f"carries {theirs!r}")
    return out


def one_state_per_record(snap: Snapshot) -> list[str]:
    """``set_state`` is single-valued, so a list of states is corruption."""
    out = []
    for type_name in snap.roi_types():
        for rec in snap.records(type_name):
            v = snap.values(rec).get(FEATURES.PIPELINE_STATE)
            if isinstance(v, (list, tuple, set)) and len(v) > 1:
                out.append(f"{rec.name} has {len(v)} states: {sorted(str(x) for x in v)}")
    return out


def vocabulary_is_present(snap: Snapshot) -> list[str]:
    """Every declared feature and type exists on the instance."""
    import lamindb as ln

    from clinicalomx.vocab.dataio import features as declared_features
    from clinicalomx.vocab.dataio import types as declared_types
    from clinicalomx.vocab.registry import discover as discover_vocab

    consumers = discover_vocab()
    want_features = {r["name"].strip() for r in declared_features()} | set(ALL_FEATURES)
    want_types = {t["name"] for t in declared_types()} | set(ALL_TYPES)
    for vocab in consumers.values():
        want_features |= set(vocab.features)
        want_types |= set(vocab.types)

    live_features = {f.name for f in ln.Feature.filter()}
    live_types = {t.name for t in ln.Record.filter(is_type=True)}
    return (
        [f"feature {n!r} is declared but absent" for n in sorted(want_features - live_features)]
        + [f"type {n!r} is declared but absent" for n in sorted(want_types - live_types)]
    )


def framework_checks() -> list[Check]:
    """The invariants the framework's own API promises."""
    return [
        Check("roi.artifacts_are_anchored", "error", artifacts_are_anchored,
              "annotate() writes roi_uid/roi_name/roi_type on every role artifact"),
        Check("roi.one_roi_per_artifact", "error", one_roi_per_artifact,
              "the back-refs are single-valued; a shared artifact belongs on the parent"),
        Check("roi.children_have_a_parent", "error", children_have_a_parent,
              "split() sets parent_roi on every non-root ROI"),
        Check("roi.parent_matches_the_name_spine", "error", parent_matches_the_name_spine,
              "parent_roi and the <a>:<b>:<c> name must agree"),
        Check("roi.names_are_unique", "error", roi_names_are_unique,
              "get_or_create is keyed by name; duplicates orphan a unit"),
        Check("roi.one_patient_per_roi", "error", one_patient_per_roi,
              "an analysis-grade ROI carries exactly one patient; a container carries none"),
        Check("roi.patient_agrees_with_the_parent", "error", patient_agrees_with_the_parent,
              "a child's patient must not contradict its parent's"),
        Check("roi.one_state_per_record", "error", one_state_per_record,
              "set_state is single-valued"),
        Check("vocab.declared_names_are_present", "error", vocabulary_is_present,
              "every declared feature and type exists on the instance (audit's MISSING half)"),
    ]
