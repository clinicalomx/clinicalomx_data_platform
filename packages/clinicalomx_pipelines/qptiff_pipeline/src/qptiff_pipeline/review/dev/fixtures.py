"""Synthetic pipeline data for local UI work: records, edit bundles and procedural images.

Nothing touches lamindb, Prefect or the HPC. The shapes match the real backends:

  - ``RECORDS``   : ``{uid: {...}}`` rows the fake lamindb serves (see ``fakes.py``).
  - ``BUNDLES``   : ``{(prefect_key_prefix, slug): bundle}`` the fake ``load_bundle`` serves,
                    each ``{"image_b64", "width", "height", "boxes"}`` like the detect flows emit.
  - ``QC_ROWS``   : ``{core_name: [row-per-channel]}`` for the bgnorm QC table.

Edit ``SLIDES`` to change the fake units, then press Reset fake data in the dev sidebar.
"""
from __future__ import annotations

import base64
import io
import json
import random

from clinicalomx import STATE_FEATURE

from qptiff_pipeline.domain import hitl, keys, states, vocab
from qptiff_pipeline.domain import qptiff as qptiff_vocab

# What to fabricate. A TMA's `state` decides which panel shows it.
SLIDES: list[dict] = [
    {
        "base": "Arutha_YTMA_471_Scan1",
        "folder": "projects",
        "image_type": vocab.MULTIPLEX,
        "n_channels": 40,
        "dims": "C=40,Y=24576,X=32768",
        "slide_state": states.S.GENERAL_APPROVED,
        "tmas": [
            {"tma": "TMA0", "state": states.S.CORE_PENDING, "grid": (4, 5), "missing": [(1, 2)]},
            {"tma": "TMA1", "state": states.S.CORE_APPROVED, "grid": (4, 6), "missing": [(0, 3), (2, 2)]},
            {"tma": "TMA2", "state": states.S.BGNORM_QUEUED, "grid": (3, 4), "missing": []},
            {"tma": "TMA3", "state": states.S.BGNORM_PENDING, "grid": (4, 4), "missing": [(3, 0)]},
            # A stranded runner batch and a terminal failure. Only the first is draggable.
            {"tma": "TMA4", "state": states.S.BGNORM_RUNNING, "grid": (3, 3), "missing": []},
            {"tma": "TMA5", "state": states.S.BGNORM_FAILED, "grid": (3, 3), "missing": [(0, 0)]},
        ],
    },
    {
        "base": "Bravo_YTMA_502_Scan1",
        "folder": "external_projects",
        "image_type": vocab.MULTIPLEX,
        "n_channels": 34,
        "dims": "C=34,Y=20480,X=28672",
        "slide_state": states.S.GENERAL_APPROVED,
        "tmas": [
            {"tma": "TMA0", "state": states.S.CORE_PENDING, "grid": (5, 6), "missing": [(4, 5)]},
            {"tma": "TMA1", "state": states.S.BGNORM_PENDING, "grid": (3, 5), "missing": []},
        ],
    },
    {
        "base": "Charlie_HE_113_Scan1",
        "folder": "projects",
        "image_type": vocab.H_AND_E,
        "n_channels": 3,
        "dims": "C=3,Y=18432,X=24576",
        "slide_state": states.S.GENERAL_APPROVED,
        "tmas": [
            {"tma": "TMA0", "state": states.S.HE_PENDING, "grid": (4, 4), "missing": []},
            {"tma": "TMA1", "state": states.S.CORE_APPROVED, "grid": (4, 4), "missing": [(1, 1)]},
        ],
    },
    # Slides waiting for general review.
    {
        "base": "Delta_YTMA_607_Scan1",
        "folder": "projects",
        "image_type": vocab.MULTIPLEX,
        "n_channels": 40,
        "dims": "C=40,Y=24576,X=32768",
        "slide_state": states.S.GENERAL_PENDING,
        "tmas": [],
    },
    {
        "base": "Echo_HE_204_Scan1",
        "folder": "external_projects",
        "image_type": vocab.H_AND_E,
        "n_channels": 3,
        "dims": "C=3,Y=16384,X=20480",
        "slide_state": states.S.GENERAL_PENDING,
        "tmas": [],
    },
]

CROP_W, CROP_H = 1000, 900       # TMA crop (core-stage review px)
THUMB_W, THUMB_H = 1100, 800     # slide thumbnail (general-stage review px)

RECORDS: dict[str, dict] = {}                  # uid -> row (see fakes._Record)
BUNDLES: dict[tuple[str, str], dict] = {}      # (prefix, slug) -> edit bundle
QC_ROWS: dict[str, list[dict]] = {}            # core name -> per-channel QC rows




def _b64_png(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# Procedural images.
def _core_image(rows: int, cols: int, missing: set, *, he: bool, seed: int):
    """A TMA crop: a jittered grid of round cores, with some cells empty.

    Returns (PIL image, [(row, col, xyxy) for the cores present]).
    """
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    img = Image.new("RGB", (CROP_W, CROP_H), (241, 237, 235) if he else (6, 8, 14))
    draw = ImageDraw.Draw(img)
    pad = 60
    pitch_x = (CROP_W - 2 * pad) / max(cols - 1, 1)
    pitch_y = (CROP_H - 2 * pad) / max(rows - 1, 1)
    r = min(pitch_x, pitch_y) * 0.36
    present = []
    for row in range(rows):
        for col in range(cols):
            if (row, col) in missing:
                continue
            cx = pad + col * pitch_x + rng.uniform(-6, 6)
            cy = pad + row * pitch_y + rng.uniform(-6, 6)
            rr = r * rng.uniform(0.82, 1.0)
            if he:  # eosin pink with a haematoxylin-like purple core
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(214, 138, 162))
                draw.ellipse([cx - rr * 0.5, cy - rr * 0.5, cx + rr * 0.5, cy + rr * 0.5],
                             fill=(150, 96, 150))
            else:  # two fluorescence channels
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(18, 120, 132))
                draw.ellipse([cx - rr * 0.6, cy - rr * 0.6, cx + rr * 0.6, cy + rr * 0.6],
                             fill=(140, 60, 150))
            present.append((row, col, [cx - rr, cy - rr, cx + rr, cy + rr]))
    return img.filter(ImageFilter.GaussianBlur(1.6)), present


def _slide_image(n_tmas: int, *, he: bool, seed: int):
    """A slide thumbnail: ``n_tmas`` TMA-shaped blocks of cores on glass. Returns (img, boxes)."""
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    img = Image.new("RGB", (THUMB_W, THUMB_H), (250, 249, 247) if he else (10, 10, 16))
    draw = ImageDraw.Draw(img)
    boxes = []
    bw = (THUMB_W - 80) / max(n_tmas, 1) - 40
    for i in range(n_tmas):
        x0 = 60 + i * (bw + 40)
        y0 = 120 + rng.uniform(-30, 30)
        x1, y1 = x0 + bw, y0 + THUMB_H * 0.6
        for row in range(6):
            for col in range(5):
                cx = x0 + 30 + col * (bw - 60) / 4
                cy = y0 + 30 + row * (y1 - y0 - 60) / 5
                rr = min((bw - 60) / 10, (y1 - y0 - 60) / 12)
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                             fill=(212, 140, 165) if he else (20, 118, 130))
        boxes.append({"class": "TMA", "xyxy": [x0 - 12, y0 - 12, x1 + 12, y1 + 12]})
    return img.filter(ImageFilter.GaussianBlur(1.2)), boxes


def _qptiff_thumb_image(*, he: bool, seed: int):
    """The qptiff's embedded RGB macro scene, shown by the triage panel."""
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    img = Image.new("RGB", (520, 380), (252, 250, 248) if he else (8, 9, 15))
    draw = ImageDraw.Draw(img)
    for row in range(7):
        for col in range(9):
            cx, cy = 60 + col * 48 + rng.uniform(-3, 3), 55 + row * 42 + rng.uniform(-3, 3)
            rr = 15
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                         fill=(216, 146, 168) if he else (24, 122, 136))
    return img.filter(ImageFilter.GaussianBlur(1.0))


# Build the fake world.
def _dearray_grid(rows: int, cols: int, present: list) -> list[dict]:
    """An accepted dearray: one box per grid cell on a regular lattice, with ``rc`` and
    ``inferred``.

    Snapped to the lattice, so it looks different from the jittered detection boxes.
    """
    detected = {(r, c) for r, c, _b in present}
    # The fitted lattice: one pitch, one cell size, every cell including the missing ones.
    pad = 60
    pitch_x = (CROP_W - 2 * pad) / max(cols - 1, 1)
    pitch_y = (CROP_H - 2 * pad) / max(rows - 1, 1)
    side = min(pitch_x, pitch_y) * 0.72
    grid = []
    for row in range(rows):
        for col in range(cols):
            cx, cy = pad + col * pitch_x, pad + row * pitch_y
            inferred = (row, col) not in detected
            grid.append({
                "class": "inferred_core" if inferred else "full_core",
                "xyxy": [cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2],
                "inferred": inferred,
                "rc": [row, col],
            })
    return grid


def _bundle_context(stage: str, unit_name: str, bundle: dict) -> dict:
    """The typed context a detect flow publishes inside its review bundle."""
    from qptiff_pipeline.domain import contexts as ctxmod

    ctx = ctxmod.CONTEXT_BY_STAGE[stage](
        unit_name=unit_name,
        raw=ctxmod.DetectionSet(
            boxes=[ctxmod.Box.from_legacy(b) for b in bundle["boxes"]],
            space=ctxmod.Space(
                name="thumbnail" if stage == "general" else "crop",
                width=bundle["width"],
                height=bundle["height"],
                scale_factor=bundle.get("scale_factor", 1.0),
                origin_x=bundle.get("crop_x", 0.0),
                origin_y=bundle.get("crop_y", 0.0),
            ),
            model_uid="fakemodeluid01",
            model_alias="production",
        ),
    )
    return json.loads(ctx.to_json())


def _qc_rows(core_name: str, n_channels: int, *, bad: bool) -> list[dict]:
    rng = random.Random(core_name)
    rows = []
    for i in range(min(n_channels, 12)):
        rows.append({
            "channel": f"CH{i:02d}",
            "background_median": round(rng.uniform(20, 240), 2),
            "signal_median": round(rng.uniform(300, 4000), 2),
            "snr": round(rng.uniform(1.2, 18.0), 2),
            "converged": not (bad and i == 3),
        })
    return rows


def build() -> None:
    """(Re)populate RECORDS, BUNDLES and QC_ROWS from the specs above. Idempotent."""
    RECORDS.clear()
    BUNDLES.clear()
    QC_ROWS.clear()

    def add(uid: str, name: str, type_name: str, values: dict) -> None:
        RECORDS[uid] = {"uid": uid, "name": name, "type": type_name, "values": values}

    n = 0
    for si, spec in enumerate(SLIDES):
        base, he = spec["base"], spec["image_type"] == vocab.H_AND_E
        # Several source folders, so the governance filters have more than one option.
        qptiff_key = f"{spec['folder']}/{base}.qptiff"
        slide_uid = f"slide{si:04d}dev0"
        slide_vals = {
            STATE_FEATURE: spec["slide_state"],
            vocab.IMAGE_TYPE: spec["image_type"],
            qptiff_vocab.QPTIFF_N_CHANNELS: spec["n_channels"],
            qptiff_vocab.QPTIFF_DIMS: spec["dims"],
            "source_image": {"__artifact__": qptiff_key},
        }
        add(slide_uid, base, states.SLIDE_TYPE, slide_vals)

        # General-stage edit bundle: TMA boxes on the slide thumbnail.
        img, boxes = _slide_image(max(len(spec["tmas"]), 2), he=he, seed=si)
        BUNDLES[(hitl.EDIT_PREFIX_BY_STAGE["general"], keys.slug(base))] = {
            "image_b64": _b64_png(img), "width": THUMB_W, "height": THUMB_H, "boxes": boxes,
        }
        # The qptiff macro thumbnail the triage panel fetches.
        BUNDLES[(hitl.QPTIFF_THUMB_PREFIX, keys.slug(base))] = {
            "image_b64": _b64_png(_qptiff_thumb_image(he=he, seed=si)),
        }

        for ti, t in enumerate(spec["tmas"]):
            tma_name = f"{base}:{t['tma']}"
            tma_uid = f"tma{si:02d}{ti:02d}dev00"
            rows, cols = t["grid"]
            missing = {tuple(m) for m in t["missing"]}
            img, present = _core_image(rows, cols, missing, he=he, seed=si * 10 + ti)
            det_boxes = [
                {"class": "full_core" if (r, c) not in missing else "partial_core", "xyxy": b}
                for r, c, b in present
            ]
            core_bundle = {
                "image_b64": _b64_png(img), "width": CROP_W, "height": CROP_H, "boxes": det_boxes,
                "scale_factor": 16.0, "crop_x": 0.0, "crop_y": 0.0,
            }
            # Half the TMAs carry a typed `context` in the bundle, half only the flat keys.
            if ti % 2 == 0:
                core_bundle["context"] = _bundle_context("core", tma_name, core_bundle)
            BUNDLES[(hitl.EDIT_PREFIX_BY_STAGE["core"], keys.slug(tma_name))] = core_bundle
            vals = {
                STATE_FEATURE: t["state"],
                vocab.IMAGE_TYPE: spec["image_type"],
                qptiff_vocab.QPTIFF_N_CHANNELS: spec["n_channels"],
                "slide": base,
                "source_image": {"__artifact__": qptiff_key},
            }
            # Anything past the core gate carries the accepted dearray grid.
            if t["state"] != states.S.CORE_PENDING:
                vals[vocab.F_CORRECTED] = json.dumps(_dearray_grid(rows, cols, present))
                vals[vocab.F_MODIFIED] = ti % 2 == 0
            add(tma_uid, tma_name, states.TMA_TYPE, vals)

            if t["state"] != states.S.BGNORM_PENDING:
                continue
            # Cores for the bgnorm panel: mostly done, some failed, some pending.
            for k, (r, c, _b) in enumerate(present):
                cname = f"{tma_name}:R{r}C{c}"
                cvals = {"is_detected": True}
                if k % 7 == 5:  # permanently failed
                    cvals[vocab.BGNORM_CORE_FAILED] = True
                    cvals[vocab.BGNORM_ATTEMPTS] = 3
                elif k % 7 == 6:  # never completed
                    cvals[vocab.BGNORM_ATTEMPTS] = 1
                else:  # bgnormed
                    cvals[vocab.BGNORMED] = {"__artifact__": f"bgnorm/{keys.slug(cname)}.zarr"}
                    QC_ROWS[cname] = _qc_rows(cname, spec["n_channels"], bad=k % 5 == 0)
                add(f"core{n:06d}dev", cname, states.CORE_TYPE, cvals)
                n += 1
