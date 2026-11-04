"""check-qptiffs: the integrity gate between register-qptiffs and mint-slides. Runs on the HPC.

Force-decodes each qptiff, tile by tile, and sets ``qptiff_integrity`` to "ok" (mint-slides mints
it) or "unreadable" (quarantined, Teams alerted). It also records the file's own metadata and one
``QptiffChannels`` Record per channel. See :func:`check_qptiffs` for the modes.
"""

from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

import os
from typing import NamedTuple

from prefect import flow, get_run_logger, task
from prefect.tasks import exponential_backoff

# Per-file peak memory is this read buffer plus one decoded tile, whatever the page size.
_DECODE_BUFFERSIZE = 64 * 1024 * 1024  # 64 MiB


def _full_res_image(node):
    """The largest image-bearing level of a scene datatree node. A copy of bgnorm_stage's."""
    nodes = [n for n in node.subtree if "image" in n.ds.variables]
    if not nodes:
        raise ValueError("scene datatree has no image-bearing levels")
    return max(nodes, key=lambda n: n.ds["image"].size).ds["image"]


def _channel_meta(node) -> dict:
    """Channel/dim metadata and the proposed image_type from a FullResolution datatree node.

    Returns an empty dict on any failure: a readable file is never marked unreadable for this.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.domain import vocab

    try:
        img = _full_res_image(node)
        cdim = next((d for d in img.dims if str(d).lower() == "c"), None)
        n_channels = int(img.sizes[cdim]) if cdim is not None else 0
        names: list[str] = []
        if cdim is not None and cdim in img.coords:
            names = [str(v) for v in img.coords[cdim].values]
        dims = ",".join(f"{d}={int(img.sizes[d])}" for d in img.dims)
        return {
            qptiff_vocab.QPTIFF_N_CHANNELS: n_channels,
            qptiff_vocab.QPTIFF_CHANNEL_NAMES: ",".join(names),
            qptiff_vocab.QPTIFF_DIMS: dims,
            vocab.IMAGE_TYPE: _infer_image_type(n_channels, names),
        }
    except Exception as exc:  # never let metadata extraction fail the integrity result
        print(f"channel-meta extraction failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
        return {}


def _thumbnail_png(datatree, *, max_px: int = 384) -> bytes | None:
    """PNG bytes of the qptiff's embedded RGB thumbnail or macro scene, downsized to ``max_px``.

    True colour, so H&E reads pink and white and multiplex dark and multicoloured. Returns None if
    no
    such scene exists or anything fails.
    """
    import io

    import numpy as np
    from PIL import Image

    try:
        cand = None
        for n in datatree.subtree:
            nm = str(getattr(n, "name", "") or "").lower()
            if ("thumb" in nm or "macro" in nm or "overview" in nm) and "image" in n.ds.variables:
                cand = n
                break
        if cand is None:
            print(f"no thumbnail scene found (nodes: {[getattr(n,'name','') for n in datatree.subtree]})", flush=True)
            return None
        arr = np.squeeze(np.asarray(cand.ds["image"].data))  # compute (small scene)
        if arr.ndim == 3:  # move a size-3/4 (RGB/RGBA) axis last, drop alpha
            ch = next((i for i, s in enumerate(arr.shape) if s in (3, 4)), None)
            if ch is not None and ch != 2:
                arr = np.moveaxis(arr, ch, 2)
            if arr.ndim == 3 and arr.shape[2] == 4:
                arr = arr[..., :3]
        elif arr.ndim == 2:  # grayscale -> RGB
            arr = np.stack([arr] * 3, axis=-1)
        if arr.ndim != 3 or arr.shape[2] != 3:
            return None
        if arr.dtype != np.uint8:  # normalise to 8-bit for display
            a = arr.astype("float32")
            mx = float(a.max()) or 1.0
            arr = (a / mx * 255.0).clip(0, 255).astype("uint8")
        im = Image.fromarray(arr, "RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        print(f"thumbnail extraction failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
        return None


def _publish_thumbnail(artifact, thumb_png: bytes, target_directory: str) -> str | None:
    """Store the qptiff thumbnail and return its Artifact key, or None.

    Writes a standalone Artifact (``<slide>/qptiff/thumbnail.png``, key recorded as
    ``qptiff_thumbnail``) and a Prefect markdown artifact ``qptiff-thumb-<slug>`` for the review
    app.
    Best effort: a failure never changes the integrity result.
    """
    import base64
    import json
    from pathlib import Path

    from qptiff_pipeline.domain import hitl, keys
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.lamin.linking import link_input
    from qptiff_pipeline.lamin.registry import get_or_create_artifact

    if not thumb_png:
        return None

    key = None
    try:
        tkey = keys.qptiff_thumbnail_key(artifact.key)
        tpath = f"{target_directory.rstrip('/')}/{tkey}"
        Path(tpath).parent.mkdir(parents=True, exist_ok=True)
        thumb_art = get_or_create_artifact(
            tpath,
            key=tkey,
            description=f"qptiff embedded RGB thumbnail for {Path(artifact.key).stem}",
            write=lambda b=thumb_png, p=tpath: Path(p).write_bytes(b),
        )
        link_input(thumb_art, artifact)  # lineage: thumbnail derived from the source qptiff
        # Not ROI-anchored: no WholeSlide exists before mint-slides, and the feature holds a key
        # string, not an Artifact.
        _mark_meta(artifact, {qptiff_vocab.QPTIFF_THUMBNAIL: tkey})  # feature holds the key string
        key = tkey
    except Exception as exc:
        print(f"thumbnail artifact write failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)

    try:
        from prefect.artifacts import create_markdown_artifact

        base = Path(artifact.key).stem
        slug = keys.slug(base)
        bundle = {"image_b64": base64.b64encode(thumb_png).decode()}
        create_markdown_artifact(
            key=hitl.QPTIFF_THUMB_PREFIX + slug,
            markdown="```json\n" + json.dumps(bundle) + "\n```",
            description=f"qptiff thumbnail (triage preview) for {base}",
        )
    except Exception as exc:
        print(f"thumbnail prefect-artifact upload failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)

    return key


def _infer_image_type(n_channels: int, channel_names: list[str]) -> str:
    """Propose H&E or multiplex from channel structure. A human confirms at triage.

    A DAPI or fluorescence channel name, or more than 3 channels, means multiplex. Exactly 3 means
    H&E. Anything else is unknown.
    """
    from qptiff_pipeline.domain import vocab

    names = [n.strip().upper() for n in (channel_names or []) if n]
    has_fluor = any("DAPI" in nm for nm in names)
    if has_fluor or (n_channels and n_channels > 3):
        return vocab.MULTIPLEX
    if n_channels == 3:
        return vocab.H_AND_E
    return vocab.IMAGE_TYPE_UNKNOWN


def _acquisition_meta(reader) -> tuple[dict, dict, list]:
    """(curated scalars, full nested blob, channel dataclasses) from the file's own PerkinElmer XML.

    Returns empty results on any failure. A file with no parseable XML yields no features.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    try:
        meta = reader.qpi_metadata
        return (
            qptiff_vocab.scalar_features(meta),
            qptiff_vocab.metadata_blob(meta),
            qptiff_vocab.channels_of(meta),
        )
    except Exception as exc:
        print(f"acquisition-meta extraction failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
        return {}, {}, []


def _write_channel_records(artifact, channels: list, *, replace: bool = False) -> int:
    """Get-or-create one ``QptiffChannels`` Record per channel and link them to the qptiff.

    Idempotent: Record names are deterministic and ``cx.get_or_create`` upserts per key. Records are
    never deleted: the instance role cannot purge orphan link rows. ``replace`` also clears the
    acquisition features the fresh read no longer answers (see :func:`_clear_stale`).
    """
    import clinicalomx as cx

    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    if not channels:
        return 0
    records = []
    for position, channel in enumerate(channels):
        name = qptiff_vocab.channel_record_name(
            artifact.key, qptiff_vocab.channel_index(channel, position))
        features = qptiff_vocab.channel_features(channel)
        fresh = dict(features)  # kept before source_artifact, which is not this file's to clear
        features["source_artifact"] = artifact  # foundation provenance, not a new role feature
        record = cx.get_or_create(name, qptiff_vocab.QPTIFF_CHANNELS_TYPE, features=features)
        if replace:
            # After the upsert, so a Record this run created exists to be read.
            _clear_stale(record, fresh, qptiff_vocab.CHANNEL_OWNED_FEATURES)
        records.append(record)

    # add_values appends, so link only what is missing. get_values() returns names, not objects.
    linked = artifact.features.get_values().get(qptiff_vocab.QPTIFF_CHANNELS) or []
    if not isinstance(linked, (list, tuple, set, frozenset)):
        linked = [linked]
    have = {str(v) for v in linked}
    fresh = [r for r in records if r.name not in have]
    if fresh:
        artifact.features.add_values({qptiff_vocab.QPTIFF_CHANNELS: fresh})
    return len(records)


class _Probe(NamedTuple):
    """Everything one file open yields."""

    pages: int          #: IFDs successfully force-decoded
    meta: dict          #: derived channel/dims/image_type features (INFERRED by this pipeline)
    thumb_png: bytes | None
    scalars: dict       #: curated acquisition features (STATED by the file)
    blob: dict          #: the full nested metadata, minus raw_xml
    channels: list      #: ChannelInfo dataclasses, one per channel


def _probe(path, *, decode: bool = True) -> _Probe:
    """Read a qptiff and return a :class:`_Probe`.

    Force-decodes every page (all pyramid levels and channels) one tile at a time, so corruption
    anywhere surfaces and memory stays bounded. ``decode=False`` reads the metadata only and
    reports ``pages=0``. A caller in that mode must not touch the integrity feature.
    """
    import tifffile
    from bioio import BioImage
    from bioio_qptiff import Reader

    p = str(path)
    # Structural check: build the scene datatree, as general-detect does. The same open reader
    # serves the channel features, the thumbnail and the acquisition XML.
    reader = BioImage(p, reader=Reader).reader
    dt = reader.xarray_dask_scene_datatree
    meta = _channel_meta(dt["FullResolution"])
    thumb_png = _thumbnail_png(dt)
    scalars, blob, channels = _acquisition_meta(reader)
    # Pixels: decode every IFD (all levels and channels), one tile or strip at a time.
    pages = 0
    if decode:
        with tifffile.TiffFile(p) as tif:
            for page in tif.pages:
                # Decode and discard each tile. maxworkers=1: the task pool supplies the
                # parallelism.
                for _ in page.segments(maxworkers=1, buffersize=_DECODE_BUFFERSIZE):
                    pass
                pages += 1
    return _Probe(pages, meta, thumb_png, scalars, blob, channels)


def reader_predates_locus(marks: dict) -> bool:
    """Whether the metadata in ``marks`` came from a reader older than the channel-locus fix.

    The fixed reader always sets ``channel_locus``, ``unknown`` included. No older reader has it.
    A refresh with an older reader in the croi image must not run: it would rewrite the old parse.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    return qptiff_vocab.QPTIFF_CHANNEL_LOCUS not in marks


def _clear_stale(holder, fresh: dict, owned) -> list[str]:
    """Remove every owned feature ``holder`` carries that the fresh read did not answer.

    Per-key writes cannot remove a value the reader stopped returning. ``owned`` limits what is
    cleared to features the qptiff's own metadata produces.
    """
    current = holder.features.get_values()
    # sorted, so the log line is stable between runs
    stale = sorted(name for name in owned if name not in fresh and current.get(name) is not None)
    for name in stale:
        holder.features.remove_values(name)
    return stale


def _mark(artifact, value: str) -> None:
    """Overwrite the single-valued integrity feature on the qptiff Artifact."""
    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    if artifact.features.get_values().get(qptiff_vocab.INTEGRITY_FEATURE) is not None:
        artifact.features.remove_values(qptiff_vocab.INTEGRITY_FEATURE)
    artifact.features.add_values({qptiff_vocab.INTEGRITY_FEATURE: value})


def _mark_meta(artifact, meta: dict) -> None:
    """Overwrite the single-valued metadata features on the qptiff Artifact. Idempotent."""
    if not meta:
        return
    current = artifact.features.get_values()
    for name in meta:
        if current.get(name) is not None:
            artifact.features.remove_values(name)
    artifact.features.add_values(dict(meta))


def _refresh_slide(artifact, marks: dict) -> list[str]:
    """Copy corrected values onto the Slide minted from this qptiff. Returns the fields written.

    Uses ``mint_slides``'s list of copied features, minus ``SLIDE_REFRESH_EXCLUDED``. The Slide is
    found by name and confirmed by ``source_image``: a stem can belong to more than one file.
    """
    from pathlib import Path

    import lamindb as ln

    from qptiff_pipeline.domain import states
    from qptiff_pipeline.flows.mint_slides import (
        SLIDE_REFRESH_EXCLUDED,
        slide_copied_features,
    )

    slide_type = ln.Record.filter(name=states.SLIDE_TYPE, is_type=True).one_or_none()
    if slide_type is None:
        return []  # nothing minted yet
    slide = ln.Record.filter(name=Path(artifact.key).stem, type=slide_type).one_or_none()
    if slide is None:
        return []  # never minted; mint-slides copies the corrected values
    values = slide.features.get_values()
    if values.get("source_image") not in (None, artifact.key):
        return []  # another file's Slide shares this stem
    wanted = {f: marks[f] for f in slide_copied_features()
              if f not in SLIDE_REFRESH_EXCLUDED and f in marks}
    changed = {f: v for f, v in wanted.items() if values.get(f) != v}
    for f in changed:
        if values.get(f) is not None:
            slide.features.remove_values(f)
    if changed:
        slide.features.add_values(dict(changed))
    return sorted(changed)


def _reconcile_declared_metadata(suffix: str) -> dict:
    """Write any declared metadata feature a previously checked qptiff is missing.

    Derived from the stored ``qptiff_metadata`` blob, with no file access. Only absent features are
    written: an existing value may be a human correction. Idempotent.
    """

    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.lamin.queries import source_artifacts

    filled, scanned = {}, 0
    for artifact in source_artifacts(suffix):
        values = artifact.features.get_values()
        blob = values.get(qptiff_vocab.QPTIFF_METADATA)
        if not isinstance(blob, dict) or not blob:
            continue  # never checked, or checked before the blob existed
        scanned += 1
        derived = qptiff_vocab.scalar_features(blob)
        missing = {k: v for k, v in derived.items() if values.get(k) is None}
        if not missing:
            continue
        artifact.features.add_values(missing)
        for name in missing:
            filled[name] = filled.get(name, 0) + 1
    return {"scanned": scanned, "filled": filled}


def _has_value(value) -> bool:
    """Whether a ``to_dataframe`` cell holds an answer. NaN and NA count as missing."""
    if value is None:
        return False
    import pandas as pd

    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return True  # arrays and lists: presence is the answer


def _reconcile_channel_records(suffix: str) -> dict:
    """Fill in any declared channel feature the existing ``QptiffChannels`` Records are missing.

    Derived from the stored blob, with no file access. Reads every Record and value once, then
    writes only the gaps. Writes a Record only if its ``source_artifact`` is the artifact being
    read:
    a stem can belong to more than one file. Skips a slide whose blob and Records disagree on
    channel
    count.
    """
    import lamindb as ln

    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.lamin.queries import source_artifacts

    out = {"scanned": 0, "skipped": 0, "not_ours": 0, "filled": {}}
    type_rec = ln.Record.filter(
        name=qptiff_vocab.QPTIFF_CHANNELS_TYPE, is_type=True).one_or_none()
    if type_rec is None:
        return out  # nothing written yet

    records = {r.name: r for r in ln.Record.filter(type=type_rec).to_list()}
    if not records:
        return out
    df = ln.Record.filter(type=type_rec).to_dataframe(features=True, limit=1_000_000)
    feature_names = {f.name for f in ln.Feature.filter()}
    columns = [c for c in df.columns if c in feature_names]
    current = {
        row.name: {c: v for c in columns if _has_value(v := getattr(row, c, None))}
        for row in df.itertuples(index=False)
    }

    for artifact in source_artifacts(suffix):
        blob = artifact.features.get_values().get(qptiff_vocab.QPTIFF_METADATA)
        if not isinstance(blob, dict) or not blob:
            continue
        channels = qptiff_vocab.channels_of(blob)
        names = [qptiff_vocab.channel_record_name(
            artifact.key, qptiff_vocab.channel_index(c, i)) for i, c in enumerate(channels)]
        if not channels or any(n not in records for n in names):
            out["skipped"] += 1  # never checked, or a partial write
            continue
        # Every Record must belong to this artifact. A shared stem resolves to another file's
        # Records.
        if any(current.get(n, {}).get("source_artifact") not in (None, artifact.key) for n in names):
            out["not_ours"] += 1
            continue
        out["scanned"] += 1
        for channel, name in zip(channels, names, strict=True):
            derived = qptiff_vocab.channel_features(channel)
            have = current.get(name, {})
            missing = {k: v for k, v in derived.items() if have.get(k) is None}
            if not missing:
                continue
            records[name].features.add_values(missing)
            for feat in missing:
                out["filled"][feat] = out["filled"].get(feat, 0) + 1
    return out


# retries: cover transient HPC-to-VM postgres errors only. Decode failures are returned, not raised.
@task(retries=4, retry_delay_seconds=exponential_backoff(backoff_factor=30), log_prints=True)
def check_one(uid: str, notify: bool, target_directory: str,
              channels: bool = True, metadata_only: bool = False,
              refresh: bool = False) -> dict:
    """Probe one qptiff Artifact, mark its integrity and alert Teams on failure.

    Each mark is committed at once, so a walltime kill resumes cleanly. Never raises on a decode
    failure. ``metadata_only`` reads the header only and leaves the integrity feature alone.
    ``refresh`` also replaces what is stored: stale features cleared, channel Records rewritten,
    Slide copies corrected.
    """
    import lamindb as ln
    from clinicalomx import governance

    from qptiff_pipeline.domain import governance as declared
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.domain import vocab
    from qptiff_pipeline.orchestration.notify import notify_teams

    # Load this repo's governance table in the task, where the policy is resolved. The SIF runs a
    # git clone with no entry point. Idempotent.
    declared.register()

    artifact = ln.Artifact.get(uid=uid)
    key = artifact.key

    # Governance: probing reads the bytes. A folder without `readable` is never opened, even if it
    # is
    # already catalogued.
    if not governance.permits_read(key):
        policy = governance.policy_for(key)
        msg = f"governance: {governance.folder_of(key) or '<root>'!r} is not readable ({policy.basis})"
        print(f"SKIP       {key}: {msg}", flush=True)
        return {"uid": uid, "key": key, "ok": None, "skipped": msg, "reason": "policy"}

    # A refresh skips the pixel pass: an earlier probe already cleared the pixels.
    header_only = metadata_only or refresh
    try:
        probe = _probe(artifact.path, decode=not header_only)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if header_only:
            # A failed metadata read never quarantines a file a full probe already passed.
            print(f"META-FAIL  {key}: {msg}", flush=True)
            return {"uid": uid, "key": key, "ok": None,
                    "skipped": f"metadata read failed: {msg}", "reason": "metadata"}
        _mark(artifact, qptiff_vocab.INTEGRITY_UNREADABLE)
        print(f"UNREADABLE {key}: {msg}", flush=True)
        # Alert per file, not at the end: the sweep can be walltime-killed.
        if notify:
            notify_teams(
                subject="[clinicalomx] unreadable qptiff quarantined",
                body=(
                    f"check-qptiffs could not read a file (marked "
                    f"'{qptiff_vocab.INTEGRITY_UNREADABLE}', will NOT be minted):\n\n"
                    f"- {key}\n    {msg}"
                ),
            )
        return {"uid": uid, "key": key, "ok": False, "error": msg}
    if not header_only:
        _mark(artifact, qptiff_vocab.INTEGRITY_OK)
    # One write pass. The three sources have disjoint keys.
    marks = dict(probe.meta)       # inferred by this pipeline: channel count, dims, image_type
    marks.update(probe.scalars)    # stated by the file: the curated acquisition scalars
    if probe.blob:
        marks[qptiff_vocab.QPTIFF_METADATA] = probe.blob  # stated by the file: everything else
    # Before any write: a refresh with an old reader leaves the stored values untouched.
    if refresh and reader_predates_locus(marks):
        msg = "reader predates the channel-locus fix; nothing to correct with it"
        print(f"STALE-READER {key}: {msg}", flush=True)
        return {"uid": uid, "key": key, "ok": None, "skipped": msg, "reason": "reader"}
    _mark_meta(artifact, marks)
    # Only a refresh clears stale features.
    cleared = _clear_stale(artifact, marks, qptiff_vocab.SCALAR_OWNED_FEATURES) if refresh else []
    n_channels = _write_channel_records(
        artifact, probe.channels, replace=refresh) if channels else 0
    slide_fields = _refresh_slide(artifact, marks) if refresh else []
    if not header_only:
        # Thumbnail: standalone Artifact and feature, plus the Prefect artifact for the review app.
        _publish_thumbnail(artifact, probe.thumb_png, target_directory)
    itype = probe.meta.get(vocab.IMAGE_TYPE, "?") if probe.meta else "?"
    label = "refreshed  " if refresh else ("meta ok    " if metadata_only else "ok         ")
    print(
        f"{label}{key} "
        f"({probe.pages} page(s) decoded; image_type={itype}; "
        f"{len(probe.scalars)} acquisition feature(s); {n_channels} channel record(s)"
        + (f"; cleared {','.join(cleared)}" if cleared else "")
        + (f"; slide {','.join(slide_fields)}" if slide_fields else "")
        + ")",
        flush=True,
    )
    return {"uid": uid, "key": key, "ok": True, "pages": probe.pages, "meta": probe.meta,
            "acquisition": len(probe.scalars), "channels": n_channels,
            "cleared": cleared, "slide_fields": slide_fields,
            # Which channel dialect this file is.
            "locus": marks.get(qptiff_vocab.QPTIFF_CHANNEL_LOCUS)}


# retries: the HPC-to-VM postgres connection can time out. File-read errors are handled per file.
@flow(name="check-qptiffs", log_prints=True, retries=3, retry_delay_seconds=60)
def check_qptiffs(
    target_directory: str,
    suffix: str = ".qptiff",
    recheck: bool = False,
    notify: bool = True,
    limit: int | None = None,
    channels: bool = True,
    metadata_only: bool = False,
    reconcile_metadata: bool = True,
    refresh_metadata: bool = False,
) -> dict:
    """Probe each qptiff Artifact's readability, mark integrity, and alert Teams on failures.

    suffix: match Artifacts whose key ends with this. Not the ``Artifact.suffix`` field.
    recheck: also re-probe files already marked ``ok`` or ``unreadable``.
    notify: send a Teams alert for each unreadable file.
    limit: cap on files probed this run (None = all).
    target_directory: results root on the HPC (a registered lamindb Storage root).
    channels: write one ``QptiffChannels`` Record per channel. The expensive half; False skips it.
    reconcile_metadata: before probing, fill declared metadata features that checked qptiffs miss,
        from the stored blob. Writes only absent features.
    metadata_only: backfill mode. Reads the header of ``ok`` files that have no
        ``qptiff_metadata``. Skips pixels and thumbnail, leaves integrity alone, ignores
        ``recheck``.
    refresh_metadata: reparse mode, after a reader change. Reads the header of ``ok`` files that
        have a blob and replaces the blob, the acquisition features, the channel Records and the
        Slide copies, clearing values the reader no longer returns. Skips the reconcile passes and
        leaves integrity alone. Cannot be combined with ``metadata_only``.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    logger = get_run_logger()

    # The two modes select opposite sets of files.
    if metadata_only and refresh_metadata:
        raise ValueError(
            "metadata_only and refresh_metadata select opposite sets (blob absent vs blob present); "
            "run them separately")

    import lamindb as ln

    # One lamindb setup for the whole batch, not per task.
    from qptiff_pipeline.domain import vocab
    from qptiff_pipeline.lamin.queries import source_artifacts
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("check-qptiffs")
    try:
        vocab.ensure()  # declares this repo's features (see vocab.py)

        # Before probe selection: carries newly declared features onto already-checked slides.
        reconciled = {"scanned": 0, "filled": {}}
        reconciled_channels = {"scanned": 0, "skipped": 0, "not_ours": 0, "filled": {}}
        # A refresh replaces the stored blob, so reconciling from it first would keep the stale
        # parse.
        if reconcile_metadata and not refresh_metadata:
            reconciled = _reconcile_declared_metadata(suffix)
            if reconciled["filled"]:
                logger.info(
                    f"reconciled {sum(reconciled['filled'].values())} missing value(s) across "
                    f"{reconciled['scanned']} checked qptiff(s): "
                    + ", ".join(f"{k}={v}" for k, v in sorted(reconciled["filled"].items())))
            else:
                logger.info(f"metadata reconcile: {reconciled['scanned']} checked qptiff(s), "
                            "nothing missing")
            if channels:
                reconciled_channels = _reconcile_channel_records(suffix)
                rc = reconciled_channels
                logger.info(
                    f"channel reconcile: {rc['scanned']} slide(s) covered, "
                    f"{rc['skipped']} skipped (no records yet or a partial write), "
                    f"{rc['not_ours']} whose channel names belong to another file, "
                    + (", ".join(f"{k}={v}" for k, v in sorted(rc["filled"].items()))
                       or "nothing missing"))

        # Select which qptiffs to probe. Skip already-marked files unless recheck.
        todo: list[str] = []
        for qptiff in source_artifacts(suffix):
            values = qptiff.features.get_values()
            current = values.get(qptiff_vocab.INTEGRITY_FEATURE)
            if metadata_only:
                # Backfill: files a full probe passed that still have no metadata blob.
                if current != qptiff_vocab.INTEGRITY_OK or values.get(qptiff_vocab.QPTIFF_METADATA) is not None:
                    continue
            elif refresh_metadata:
                # Reparse: files a full probe passed that have a blob.
                if current != qptiff_vocab.INTEGRITY_OK or values.get(qptiff_vocab.QPTIFF_METADATA) is None:
                    continue
            elif current is not None and not recheck:
                continue  # already checked (ok or unreadable); recheck=True re-probes
            todo.append(qptiff.uid)
            if limit and len(todo) >= limit:
                break

        batch = int(os.environ.get("SLURM_CPUS_PER_TASK", "4"))
        mode = ("refreshing metadata for" if refresh_metadata
                else "reading metadata for" if metadata_only else "probing")
        logger.info(f"{mode} {len(todo)} qptiff(s), up to {batch} concurrently")

        # Fan out across files. Each decode is memory-bounded, so `batch` files fit in memory.
        results: list[dict] = []
        for i in range(0, len(todo), batch):
            futures = [
                check_one.submit(uid, notify, target_directory, channels, metadata_only,
                                 refresh_metadata)
                for uid in todo[i : i + batch]
            ]
            for fut in futures:
                res = fut.result(raise_on_failure=False)
                if isinstance(res, dict):
                    results.append(res)
                else:
                    # The task failed after retries. The file stays unchecked for the next run.
                    logger.error(f"check_one task failed after retries: {res!r}")
            logger.info(f"processed {min(i + batch, len(todo))}/{len(todo)}")

        # Four outcomes. `ok is None` means no verdict, bucketed by reason: policy or metadata read.
        n_ok = sum(1 for r in results if r.get("ok") is True)
        bad = [r["key"] for r in results if r.get("ok") is False]
        no_verdict = [r for r in results if r.get("ok") is None]
        skipped = [r["key"] for r in no_verdict if r.get("reason") == "policy"]
        meta_failed = [r["key"] for r in no_verdict if r.get("reason") == "metadata"]
        stale_reader = [r["key"] for r in no_verdict if r.get("reason") == "reader"]
        n_channels = sum(r.get("channels") or 0 for r in results)
        # Report what a refresh changed, not only what it covered.
        cleared: dict[str, int] = {}
        slide_fields: dict[str, int] = {}
        loci: dict[str, int] = {}
        for r in results:
            for name in r.get("cleared") or []:
                cleared[name] = cleared.get(name, 0) + 1
            for name in r.get("slide_fields") or []:
                slide_fields[name] = slide_fields.get(name, 0) + 1
            if r.get("ok") is True:
                # An unrecognised locus is counted under its own name.
                loci[r.get("locus") or "not reported"] = loci.get(r.get("locus") or "not reported", 0) + 1
        logger.info(
            f"check-qptiffs done: checked={len(results) - len(no_verdict)} ok={n_ok} "
            f"unreadable={len(bad)} skipped_by_policy={len(skipped)} "
            f"metadata_failed={len(meta_failed)} channel_records={n_channels} "
            f"stale_reader={len(stale_reader)}"
        )
        if stale_reader:
            logger.error(
                f"{len(stale_reader)} file(s) NOT refreshed: this run's reader predates the "
                "channel-locus fix, so it would have rewritten the same values it was meant to "
                "correct. Rebuild the croi image and repoint the deployment at it, then re-run - "
                "nothing was changed, so the re-run has the same work to do.")
        if refresh_metadata:
            logger.info(
                "refresh: cleared "
                + (", ".join(f"{k}={v}" for k, v in sorted(cleared.items())) or "nothing")
                + "; slides updated "
                + (", ".join(f"{k}={v}" for k, v in sorted(slide_fields.items())) or "none")
                + "; channel locus "
                + (", ".join(f"{k}={v}" for k, v in sorted(loci.items())) or "none"))
            unplaced = loci.get(qptiff_vocab.LOCUS_UNKNOWN, 0) + loci.get("not reported", 0)
            if unplaced:
                logger.warning(
                    f"{unplaced} file(s) the reader could not place: their per-channel fields are the "
                    "least trustworthy on the instance and are worth a look before they are relied on")
        return {"checked": len(results) - len(no_verdict), "ok": n_ok,
                "unreadable": bad, "skipped_by_policy": skipped,
                "metadata_failed": meta_failed, "stale_reader": stale_reader,
                "channel_records": n_channels,
                "reconciled": reconciled, "reconciled_channels": reconciled_channels,
                "cleared": cleared, "slide_fields": slide_fields, "locus": loci}
    finally:
        ln.finish()
