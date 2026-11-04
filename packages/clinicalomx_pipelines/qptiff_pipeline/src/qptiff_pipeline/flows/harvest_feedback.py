"""harvest-feedback: collect human-corrected accepts into a training-reference Collection per stage.

References the accepted-labels artifact (``roi_accepted`` or ``cores_accepted``) of every accept a
reviewer edited (``roi_modified``). Metadata only: no bytes are copied. Only scans whose governance
policy permits ``training`` are included. Runs on the VM.

``stage`` is "general", "core" or "all". ``dry_run`` defaults True.
"""

from __future__ import annotations

# Put the clone's src/ on sys.path: the worker runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

# Per stage: the accepted-labels feature to reference, and the Collection key.
_STAGE_CFG = {
    "general": {"accepted_feature": "roi_accepted", "collection": "clinical-roi/general/feedback"},
    "core": {"accepted_feature": "cores_accepted", "collection": "clinical-roi/core/feedback"},
}


def _harvest_stage(stage: str, dry_run: bool, limit: int | None, logger) -> dict:
    """(Re)build one stage's training-reference Collection from modified-accepted units."""
    import lamindb as ln
    from clinicalomx import get_state, governance

    from qptiff_pipeline.domain import governance as declared
    from qptiff_pipeline.domain import states, vocab

    # Load the governance table: without an entry point, `training` is denied everywhere and the
    # harvest is silently empty.
    declared.register()

    cfg = _STAGE_CFG[stage]
    gate = states.GATES[stage]
    approved = gate.approved
    accepted_feature = cfg["accepted_feature"]
    coll_key = cfg["collection"]

    def _source_key(value) -> str | None:
        """The source image's key. Feature values arrive as an Artifact or its key string."""
        if value is None:
            return None
        return getattr(value, "key", None) or str(value)

    unit_type = ln.Record.filter(name=gate.unit_type, is_type=True).one_or_none()
    pool = list(ln.Record.filter(type=unit_type).all()) if unit_type else []

    members: dict[str, object] = {}  # uid -> Artifact (dedup)
    referenced: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for unit in pool:
        if get_state(unit) != approved:
            continue
        v = unit.features.get_values()
        if not bool(v.get(vocab.F_MODIFIED)):
            continue  # only human-edited accepts

        # Governance gate: a training-reference Collection feeds a redistributed model. Resolved
        # from the source image's key. An undeclared folder is excluded and reported.
        source_key = _source_key(v.get("source_image"))
        if source_key is None:
            skipped.append((unit.name, "no source_image; cannot resolve a governance policy"))
            continue
        policy = governance.policy_for(source_key)
        if not policy.training:
            folder = governance.folder_of(source_key) or "<root>"
            skipped.append((
                unit.name,
                f"governance: {folder!r} does not permit training use ({policy.basis})",
            ))
            continue

        art = v.get(accepted_feature)
        art = art if isinstance(art, ln.Artifact) else (ln.Artifact.filter(key=art, is_latest=True).one_or_none() if art else None)
        if art is None:
            skipped.append((unit.name, f"no {accepted_feature} artifact"))
            continue
        members[art.uid] = art
        referenced.append({"unit": unit.name, "uid": unit.uid, "artifact_key": art.key})
        if limit and len(members) >= limit:
            break

    logger.info(
        f"[{stage}] {len(referenced)} modified-accepted {gate.unit_type}(s) -> "
        f"Collection {coll_key!r}; skipped {len(skipped)}"
    )
    for name, why in skipped:
        logger.info(f"  skip {name}: {why}")

    if dry_run:
        return {"stage": stage, "dry_run": True, "would_reference": len(members),
                "skipped": len(skipped), "collection": coll_key, "units": referenced}

    # Revise the latest Collection version. An unchanged member set is a no-op.
    latest = ln.Collection.filter(key=coll_key, is_latest=True).one_or_none()
    prior = {a.uid for a in latest.ordered_artifacts} if latest else set()
    if latest is not None and set(members) == prior:
        logger.info(f"[{stage}] {coll_key}: unchanged ({len(members)} references)")
        return {"stage": stage, "dry_run": False, "referenced": len(members),
                "skipped": len(skipped), "collection": coll_key, "collection_uid": latest.uid,
                "changed": False}
    coll = ln.Collection(
        list(members.values()), key=coll_key, revises=latest,
        description=(f"Training references: human-EDITED accepted {gate.unit_type} labels "
                     f"({accepted_feature}). Convert via the training io util."),
    ).save()
    logger.info(f"[{stage}] {coll_key}: {len(members)} references (uid={coll.uid})")
    return {"stage": stage, "dry_run": False, "referenced": len(members),
            "skipped": len(skipped), "collection": coll_key, "collection_uid": coll.uid,
            "changed": True}


@flow(name="harvest-feedback", log_prints=True)
def harvest_feedback(stage: str = "all", dry_run: bool = True, limit: int | None = None) -> dict:
    """Collect modified-accepted labels into the per-stage training-reference Collection(s).

    stage: "general", "core", or "all".
    dry_run: True (default) reports what it would collect. False rebuilds the Collection.
    limit: cap on references per stage.
    """
    logger = get_run_logger()

    import lamindb as ln

    from qptiff_pipeline.lamin.tracking import track_prefect_run

    stages = ["general", "core"] if stage == "all" else [stage]
    for s in stages:
        if s not in _STAGE_CFG:
            raise ValueError(f"stage must be one of {list(_STAGE_CFG)} or 'all', got {stage!r}")

    track_prefect_run("harvest-feedback")
    try:
        results = [_harvest_stage(s, dry_run, limit, logger) for s in stages]
        logger.info(f"harvest-feedback done: stages={stages} dry_run={dry_run}")
        return {"dry_run": dry_run, "stages": results}
    finally:
        ln.finish()
