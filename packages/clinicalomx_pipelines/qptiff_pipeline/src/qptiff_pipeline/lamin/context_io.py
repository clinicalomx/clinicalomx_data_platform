"""Read and write a :mod:`contexts` model on a lamindb Record, and publish it to the review app.

``read_context``    the stored context, or one assembled from the older per-feature layout.
``write_context``   stores a JSON feature on the Record and a ``context.json`` Artifact.
``publish_context`` the Prefect artifact that carries the image bytes to the review app.
"""
from __future__ import annotations

import json
import logging

from qptiff_pipeline.domain import contexts as ctxmod
from qptiff_pipeline.domain import hitl, vocab

# One JSON-string feature per stage, holding the whole context minus image bytes.
CONTEXT_FEATURE_BY_STAGE: dict[str, str] = {
    "general": vocab.GENERAL_CONTEXT,
    "core": vocab.CORE_CONTEXT,
}
CONTEXT_ARTIFACT_FEATURE = vocab.DETECT_CONTEXT  # the durable context.json pointer

log = logging.getLogger("qptiff_pipeline.lamin.context_io")


def context_feature(stage: str) -> str:
    return CONTEXT_FEATURE_BY_STAGE[stage]


def ensure_context_features(stage: str) -> None:
    """Declare the context features. Idempotent."""
    from qptiff_pipeline.domain import vocab

    vocab.ensure()
    _ = stage  # accepted for call-site symmetry; the whole vocabulary is declared at once


def _resolve_artifact(value):
    """The current Artifact behind a stored pointer: an Artifact, a key string or a list."""
    from clinicalomx import newest_artifact

    return newest_artifact(value)


def to_artifact_ref(value) -> ctxmod.ArtifactRef | None:
    """An artifact pointer (Artifact or key string) -> :class:`contexts.ArtifactRef`."""
    if value is None:
        return None
    key = getattr(value, "key", None)
    uid = getattr(value, "uid", None)
    if key is None and uid is None:
        return ctxmod.ArtifactRef(key=str(value))
    return ctxmod.ArtifactRef(key=key, uid=uid)


# build (detect side)
def build_detect_context(
    stage: str,
    *,
    unit_name: str,
    unit_uid: str | None,
    gdf,
    width: int,
    height: int,
    scale_factor: float,
    crop_x: float = 0.0,
    crop_y: float = 0.0,
    source_image=None,
    model_uid: str | None = None,
    model_alias: str | None = None,
    params: dict | None = None,
    images: dict | None = None,
):
    """Assemble a context from a detect flow's outputs, in review px.

    ``gdf`` holds ``class``/``conf``/``geometry``. ``images`` maps an ImageRef kind to a pointer.
    """
    space = ctxmod.Space(
        name="thumbnail" if stage == "general" else "crop",
        width=int(width),
        height=int(height),
        scale_factor=float(scale_factor),
        origin_x=float(crop_x),
        origin_y=float(crop_y),
    )
    boxes = [
        ctxmod.Box.from_legacy({
            "class": r["class"],
            "conf": None if r.get("conf") is None else float(r["conf"]),
            "xyxy": list(r.geometry.bounds),
        })
        for _i, r in gdf.iterrows()
        if r.geometry is not None
    ]
    ctx = ctxmod.CONTEXT_BY_STAGE[stage](
        unit_name=unit_name,
        unit_uid=unit_uid,
        source_image=to_artifact_ref(source_image),
        raw=ctxmod.DetectionSet(
            boxes=boxes,
            space=space,
            model_uid=model_uid,
            model_alias=model_alias,
            params={k: float(v) for k, v in (params or {}).items() if v is not None},
        ),
    )
    for kind, art in (images or {}).items():
        ref = to_artifact_ref(art)
        if ref is not None:
            ctx.images.append(ctxmod.ImageRef(kind=kind, ref=ref))
    return ctx


def transport_payload(ctx, *, image_b64: str) -> dict:
    """The Prefect review-bundle payload: the flat keys older readers parse, plus ``context``.

    The image bytes appear once, at the top level. ``context`` is dumped without them.
    """
    payload = {
        "scale_factor": float(ctx.raw.space.scale_factor),
        "width": ctx.raw.space.width,
        "height": ctx.raw.space.height,
        "boxes": [
            {
                "class": b.cls,
                **({} if b.conf is None else {"conf": round(b.conf, 3)}),
                "xyxy": [round(v, 1) for v in b.xyxy],
            }
            for b in ctx.raw.boxes
        ],
        "image_b64": image_b64,
        "context": json.loads(ctx.to_json()),
    }
    if ctx.raw.space.origin_x or ctx.raw.space.origin_y:
        payload["crop_x"] = ctx.raw.space.origin_x
        payload["crop_y"] = ctx.raw.space.origin_y
    return payload


# read
def read_context(record, stage: str, *, bundle: dict | None = None):
    """The unit's context, or None if nothing can be assembled.

    Tries the stored context feature, then the older features. ``bundle`` is a review bundle the
    caller already fetched; without it the candidate parquet is read.
    """
    vals = record.features.get_values()
    raw = vals.get(context_feature(stage))
    if raw:
        try:
            return ctxmod.parse_context(stage, raw)
        except Exception as exc:  # noqa: BLE001 - version skew must degrade, not fail
            # Logged: a context that fails to parse falls back to the older features below.
            log.warning("stored %s context for %s did not parse (%s); using the legacy features",
                        stage, getattr(record, "name", "?"), exc)
    return _from_legacy(record, stage, vals, bundle)


def _space_from(width, height, attrs: dict, stage: str) -> ctxmod.Space:
    """Build a :class:`contexts.Space` from parquet ``.attrs`` or bundle keys.

    An accepted parquet stores 1.0 as ``scale_factor`` and the real factor as
    ``source_scale_factor``. Both resolve to "multiply to reach full-res".
    """
    factor = attrs.get("scale_factor")
    if factor in (None, 1.0) and attrs.get("source_scale_factor"):
        factor = attrs["source_scale_factor"]
    return ctxmod.Space(
        name="thumbnail" if stage == "general" else "crop",
        width=int(width),
        height=int(height),
        scale_factor=float(factor or 1.0),
        origin_x=float(attrs.get("crop_x", 0.0) or 0.0),
        origin_y=float(attrs.get("crop_y", 0.0) or 0.0),
    )


def _from_legacy(record, stage: str, vals: dict, bundle: dict | None):
    """Assemble a context out of the pre-context features. Returns None if there is no raw state."""
    raw_boxes: list[ctxmod.Box] = []
    attrs: dict = {}
    width = height = None

    if bundle and bundle.get("context"):
        # Take the raw half from the bundle's typed context. The accepted half only lives on the
        # Record and is attached below.
        try:
            ctx = ctxmod.parse_context(stage, bundle["context"])
            ctx.accepted = None
            return _attach_legacy_accepted(ctx, vals)
        except Exception as exc:  # noqa: BLE001 - fall back to the flat bundle keys
            log.warning("bundle context for %s did not parse (%s); using the flat bundle keys",
                        getattr(record, "name", "?"), exc)

    if bundle:  # the review app's path: the edit bundle it already fetched
        raw_boxes = [ctxmod.Box.from_legacy(b) for b in bundle.get("boxes", [])]
        width, height = bundle.get("width"), bundle.get("height")
        attrs = {k: bundle[k] for k in ("scale_factor", "crop_x", "crop_y") if k in bundle}
    else:  # server-side: the candidate prediction parquet
        pred = _resolve_artifact(vals.get(vocab.F_PRED))
        if pred is not None:
            from qptiff_pipeline.domain.roi_io import read_roi_parquet

            gdf = read_roi_parquet(pred.path)
            attrs = dict(gdf.attrs)
            width = attrs.get("thumb_w")
            height = attrs.get("thumb_h")
            raw_boxes = [
                ctxmod.Box.from_legacy({
                    "class": r["class"],
                    "conf": r.get("conf"),
                    "xyxy": list(r.geometry.bounds),
                })
                for _i, r in gdf.iterrows()
                if r.geometry is not None
            ]
    if width is None or height is None:
        return None  # no raw state to anchor a space on

    space = _space_from(width, height, attrs, stage)
    model = ctxmod.CONTEXT_BY_STAGE[stage]
    ctx = model(
        unit_name=record.name,
        unit_uid=getattr(record, "uid", None),
        source_image=to_artifact_ref(vals.get("source_image")),
        raw=ctxmod.DetectionSet(boxes=raw_boxes, space=space),
    )

    return _attach_legacy_accepted(ctx, vals)


def _attach_legacy_accepted(ctx, vals: dict):
    """Attach the accepted state and image pointers from the older features onto ``ctx``."""
    corrected = vals.get(vocab.F_CORRECTED)
    if corrected:
        ctx.accepted = ctxmod.AcceptedSet(
            boxes=[ctxmod.Box.from_legacy(b) for b in json.loads(corrected)],
            space=ctx.raw.space,
            modified=bool(vals.get(vocab.F_MODIFIED)),
        )
    have = {im.kind for im in ctx.images}
    for feat, kind in (
        (vocab.F_THUMBNAIL, "clean"),
        (vocab.F_DET_OVERLAY, "detection_overlay"),
        (vocab.F_DEARRAY_OVERLAY, "dearray_overlay"),
    ):
        ref = to_artifact_ref(vals.get(feat))
        if ref is not None and kind not in have:
            ctx.images.append(ctxmod.ImageRef(kind=kind, ref=ref))
    return ctx


# write
def write_context(record, ctx, *, target_directory: str | None = None, qptiff_key: str | None = None):
    """Store ``ctx`` on ``record``: the JSON feature always, the ``context.json`` Artifact too when
    ``target_directory`` and ``qptiff_key`` are given (on the HPC, where storage is writable).
    """
    ensure_context_features(ctx.stage)
    payload = ctx.to_json()  # image bytes stripped: a Record feature stays a few KB
    _set_feature(record, context_feature(ctx.stage), payload)

    if target_directory and qptiff_key:
        art = _write_context_artifact(ctx, payload, target_directory, qptiff_key)
        if art is not None:
            # Anchor the context.json to its ROI, like every other derivative.
            from clinicalomx import annotate

            annotate(record, art, CONTEXT_ARTIFACT_FEATURE)
    return record


def _set_feature(record, name: str, value) -> None:
    """Overwrite a single-valued feature. lamindb features are multi-valued: add alone appends."""
    if record.features.get_values().get(name) is not None:
        record.features.remove_values(name)
    record.features.add_values({name: value})


def _write_context_artifact(ctx, payload: str, target_directory: str, qptiff_key: str):
    """Register ``<unit dir>/context.json``. Best effort: never fails an Accept."""
    from pathlib import Path

    from qptiff_pipeline.domain import keys
    from qptiff_pipeline.lamin.registry import get_or_create_artifact

    try:
        key = (keys.general_key(qptiff_key, "context") if ctx.stage == "general"
               else keys.core_key(qptiff_key, ctx.unit_name, "context"))
        local = Path(target_directory) / key
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(payload)
        return get_or_create_artifact(
            str(local), key, description=f"{ctx.stage} detect context for {ctx.unit_name}"
        )
    except Exception as exc:  # noqa: BLE001 - the durable copy is best effort; the Record holds the JSON
        log.warning("could not write context.json for %s: %s", ctx.unit_name, exc)
        return None


# publish (the review app's transport)
def publish_context(ctx, *, image_b64: str, slug: str | None = None) -> None:
    """Publish the review bundle for ``ctx`` as the keyed Prefect markdown artifact.

    ``slug`` overrides the key suffix for the general stage, whose key uses the qptiff stem.
    """
    from prefect.artifacts import create_markdown_artifact

    from qptiff_pipeline.domain import keys

    payload = transport_payload(ctx, image_b64=image_b64)
    create_markdown_artifact(
        key=hitl.EDIT_PREFIX_BY_STAGE[ctx.stage] + (slug or keys.slug(ctx.unit_name)),
        markdown="```json\n" + json.dumps(payload) + "\n```",
        description=f"{ctx.stage} edit bundle (editor-facing) for {ctx.unit_name}",
    )
