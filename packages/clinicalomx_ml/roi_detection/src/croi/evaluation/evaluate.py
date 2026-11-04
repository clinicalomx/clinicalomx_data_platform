"""Score a detector for any objective: recall and IoU at several thresholds, per class and
modality, and a worst-prediction contact sheet.

Everything up to ``flatten_for_mlflow`` is a pure scorer over (image, label) path pairs with no
lamindb or MLflow. ``evaluate_model`` is the governed step that resolves artifacts and logs.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from statistics import median

import lamindb as ln
import mlflow
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from croi import feature_names, objectives
from croi.detection.backends import detector_for
from croi.detection.detector import ObjectDetector
from croi.detection.tracking import log_collection_inputs
from croi.evaluation.plots import (
    iou_distribution,
    per_image_iou_scatter,
    recall_by_modality,
)
from croi.io.registries import framework_of

IOU_THRESHOLDS = (0.5, 0.7, 0.9)
_HE_PATTERN = re.compile(r"(?:^|[_\-\s])(H&E|HandE|HE)(?:[_\-\s\.]|$)", re.IGNORECASE)


def infer_modality(stem: str) -> str:
    """mIF or H&E from the filename. Heuristic."""
    return "H&E" if _HE_PATTERN.search(stem) else "mIF"


def yolo_to_xyxy(line: str, img_w: int, img_h: int):
    parts = line.split()
    if len(parts) < 5:
        return None
    cls = int(parts[0])
    cx, cy, w, h = (float(x) for x in parts[1:5])
    return cls, [(cx - w / 2) * img_w, (cy - h / 2) * img_h,
                 (cx + w / 2) * img_w, (cy + h / 2) * img_h]


def iou_xyxy(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match_greedy(preds, gts, iou_thr, *, class_agnostic=False):
    """Greedy IoU matching of preds to gts. Returns ``(matched_ious, matched_classes, fp, fn)``.

    By default a pred matches only a gt of the same class; ``class_agnostic`` ignores class.
    """
    sorted_preds = sorted(preds, key=lambda p: -p[2])
    matched = [False] * len(gts)
    matched_ious: list[float] = []
    matched_classes: list[int] = []
    fp = 0
    for cls, box, _ in sorted_preds:
        best_i, best_iou = -1, iou_thr
        for i, (gt_cls, gt_box) in enumerate(gts):
            if matched[i] or (not class_agnostic and gt_cls != cls):
                continue
            v = iou_xyxy(box, gt_box)
            if v > best_iou:
                best_iou, best_i = v, i
        if best_i >= 0:
            matched[best_i] = True
            matched_ious.append(best_iou)
            matched_classes.append(cls)
        else:
            fp += 1
    return matched_ious, matched_classes, fp, matched.count(False)


def aggregate(rows, class_names):
    def metrics_for(subset, *, agnostic=False):
        tpk, fpk, fnk = ("tp_det", "fp_det", "fn_det") if agnostic else ("tp", "fp", "fn")
        iouk = "matched_ious_det_05" if agnostic else "matched_ious_05"
        out = {}
        n_images = len(subset)
        for thr in IOU_THRESHOLDS:
            tp = sum(r[tpk][thr] for r in subset)
            fp = sum(r[fpk][thr] for r in subset)
            fn = sum(r[fnk][thr] for r in subset)
            out[f"recall@{thr}"] = tp / (tp + fn) if (tp + fn) else float("nan")
            out[f"precision@{thr}"] = tp / (tp + fp) if (tp + fp) else float("nan")
            denom = float(n_images) if n_images else float("nan")
            out[f"tp_per_image@{thr}"] = tp / denom if n_images else float("nan")
            out[f"fp_per_image@{thr}"] = fp / denom if n_images else float("nan")
            out[f"fn_per_image@{thr}"] = fn / denom if n_images else float("nan")
        ious = [v for r in subset for v in r[iouk]]
        out["iou_median"] = median(ious) if ious else float("nan")
        out["iou_p10"] = float(np.percentile(ious, 10)) if ious else float("nan")
        out["n_images"] = n_images
        return out

    # `detection` (class-agnostic) is the primary metric; `overall` and `class:*` are class-aware.
    summary = {"detection": metrics_for(rows, agnostic=True)}
    for mod in ("mIF", "H&E"):
        summary[f"detection:{mod}"] = metrics_for(
            [r for r in rows if r["modality"] == mod], agnostic=True
        )
    summary["overall"] = metrics_for(rows)
    for cls_id, name in class_names.items():
        summary[f"class:{name}"] = metrics_for(
            [r for r in rows if cls_id in r["gt_classes"] or cls_id in r["pred_classes"]]
        )
    for mod in ("mIF", "H&E"):
        summary[f"modality:{mod}"] = metrics_for([r for r in rows if r["modality"] == mod])
    return summary


def render_contact_sheet(rows, out_path, n, class_names):
    def worst_key(r):
        if r["fn"][0.5] > 0:
            return (-1.0, r["image_path"])
        if not r["matched_ious_05"]:
            return (1.0, r["image_path"])
        return (min(r["matched_ious_05"]), r["image_path"])

    worst = sorted(rows, key=worst_key)[:n]
    if not worst:
        return
    cols = 4
    rows_n = (len(worst) + cols - 1) // cols
    cell = 320
    sheet = Image.new("RGB", (cols * cell, rows_n * cell), (20, 20, 20))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    for idx, r in enumerate(worst):
        img = Image.open(r["image_path"]).convert("RGB")
        scale = cell / max(img.size)
        img = img.resize((round(img.size[0] * scale), round(img.size[1] * scale)), Image.LANCZOS)
        draw = ImageDraw.Draw(img)
        for cls, box in r["gts_xyxy"]:
            draw.rectangle([v * scale for v in box], outline=(255, 80, 80), width=2)
        for cls, box, conf in r["preds_xyxy"]:
            sb = [v * scale for v in box]
            draw.rectangle(sb, outline=(80, 255, 120), width=2)
            draw.text((sb[0] + 2, sb[1] + 2), f"{class_names.get(cls, cls)} {conf:.2f}",
                      fill=(80, 255, 120), font=font)
        draw.text((4, 4),
                  f"{Path(r['image_path']).stem[:40]}  [{r['modality']}]  "
                  f"miss={r['fn'][0.5]} fp={r['fp'][0.5]}",
                  fill=(255, 255, 255), font=font)
        cy, cx = (idx // cols) * cell, (idx % cols) * cell
        sheet.paste(img, (cx + (cell - img.size[0]) // 2, cy + (cell - img.size[1]) // 2))
    sheet.save(out_path)


def score_pairs(detector: ObjectDetector, pairs, class_names, *, imgsz, conf):
    """Run an ``ObjectDetector`` over ``(image_path, label_path)`` pairs. Returns ``(summary, rows)``."""
    rows: list[dict] = []
    for img_path, lbl_path in pairs:
        img_path, lbl_path = Path(img_path), Path(lbl_path)
        with Image.open(img_path) as im:
            img_w, img_h = im.size
        gts = [g for g in (yolo_to_xyxy(line, img_w, img_h)
                           for line in lbl_path.read_text().splitlines()) if g is not None]
        preds = [(d.class_id, list(d.xyxy), d.confidence)
                 for d in detector.predict(str(img_path), conf=conf, imgsz=imgsz)]
        row = {
            "image_path": str(img_path),
            "modality": infer_modality(img_path.stem),
            "gt_classes": [c for c, _ in gts],
            "pred_classes": [c for c, _, _ in preds],
            "gts_xyxy": gts, "preds_xyxy": preds,
            "tp": {}, "fp": {}, "fn": {},                  # class-aware
            "tp_det": {}, "fp_det": {}, "fn_det": {},      # class-agnostic
            "matched_ious_05": [], "matched_classes_05": [],
            "matched_ious_det_05": [],
        }
        for thr in IOU_THRESHOLDS:
            ious, classes, fp, fn = match_greedy(preds, gts, thr)
            row["tp"][thr], row["fp"][thr], row["fn"][thr] = len(ious), fp, fn
            d_ious, _, d_fp, d_fn = match_greedy(preds, gts, thr, class_agnostic=True)
            row["tp_det"][thr], row["fp_det"][thr], row["fn_det"][thr] = len(d_ious), d_fp, d_fn
            if thr == 0.5:
                row["matched_ious_05"], row["matched_classes_05"] = ious, classes
                row["matched_ious_det_05"] = d_ious
        rows.append(row)
    return aggregate(rows, class_names), rows


def write_eval_outputs(summary, rows, class_names, out_dir, worst_n) -> list[Path]:
    """Write metrics.json, per_image.csv, the contact sheet and plots. Returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(summary, indent=2,
                   default=lambda o: float(o) if isinstance(o, np.floating) else str(o))
    )
    with (out_dir / "per_image.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image", "modality", "n_gt", "n_pred"]
                   + [f"tp@{t}" for t in IOU_THRESHOLDS]
                   + [f"fp@{t}" for t in IOU_THRESHOLDS]
                   + [f"fn@{t}" for t in IOU_THRESHOLDS] + ["min_matched_iou"])
        for r in rows:
            w.writerow([Path(r["image_path"]).name, r["modality"],
                        len(r["gts_xyxy"]), len(r["preds_xyxy"])]
                       + [r["tp"][t] for t in IOU_THRESHOLDS]
                       + [r["fp"][t] for t in IOU_THRESHOLDS]
                       + [r["fn"][t] for t in IOU_THRESHOLDS]
                       + [min(r["matched_ious_05"]) if r["matched_ious_05"] else ""])
    render_contact_sheet(rows, out_dir / "worst_predictions.png", worst_n, class_names)
    recall_by_modality(summary, out_dir / "recall_by_modality.png")
    iou_distribution(rows, class_names, out_dir / "iou_distribution.png")
    per_image_iou_scatter(rows, out_dir / "per_image_iou_scatter.png")
    names = ["metrics.json", "per_image.csv", "worst_predictions.png",
             "recall_by_modality.png", "iou_distribution.png", "per_image_iou_scatter.png"]
    return [out_dir / n for n in names if (out_dir / n).exists()]


def flatten_for_mlflow(summary) -> dict[str, float]:
    """Flatten a summary to ``{sanitized_metric_name: value}`` for mlflow.log_metric."""
    def _san(name: str) -> str:
        return name.replace(":", "/").replace("@", "_").replace("&", "").replace(" ", "_")
    out: dict[str, float] = {}
    for section, m in summary.items():
        for k, v in m.items():
            if isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v)):
                out[f"{_san(section)}/{_san(k)}"] = float(v)
    return out


def print_summary(summary, weights_label: str) -> None:
    n = summary["overall"]["n_images"]
    print(f"\nEval ({n} images) against {weights_label}")
    print("=" * 80)
    for section, m in summary.items():
        print(f"\n[{section}]  n_images={m['n_images']}")
        for thr in IOU_THRESHOLDS:
            print(f"  IoU>={thr}:  recall={m[f'recall@{thr}']:.3f}  "
                  f"precision={m[f'precision@{thr}']:.3f}  "
                  f"tp/img={m[f'tp_per_image@{thr}']:.2f} "
                  f"fp/img={m[f'fp_per_image@{thr}']:.2f} "
                  f"fn/img={m[f'fn_per_image@{thr}']:.2f}")
        print(f"  IoU dist (matched@0.5): median={m['iou_median']:.3f}  p10={m['iou_p10']:.3f}")


# --- the governed step ------------------------------------------------------------------

# Pinned uid: a source change versions this file's Transform instead of forking it.
@ln.step(uid="3DdrP0t3bfCL")
def evaluate_model(
    objective: str,
    config: dict,
    weights_art: ln.Artifact,
    output_storage: ln.Storage,
) -> ln.Artifact:
    """Score the champion on the held-out eval split and on all data, each in its own MLflow run.

    ``all`` includes train, so it is a sanity view; the gap to the eval split is the
    generalization gap. Returns the eval split's metrics Artifact.
    """
    params = config["parameters"]
    eval_config = params["eval"]
    outputs = config["outputs"]
    run_label = params["name"]

    data_dir = output_storage.path / config["data_dir"]
    dataset = objectives.get(objective).load_dataset(config)
    categories = dict(enumerate(dataset.categories))
    storage_root = Path(str(output_storage.path))
    ver = weights_art.version

    split = eval_config.get("split", "test")
    co = bool(eval_config.get("co_occurrence", False))
    eval_label = "co_occurrence" if co else split
    experiment = eval_config["mlflow_experiment"]

    views = [
        (eval_label, dataset.of("co_occurrence" if co else split)),
        ("all", dataset.of("all")),
    ]

    # Score raw predictions (`contained_thr: 0`).
    framework = framework_of(weights_art)
    detector = detector_for({"framework": framework, "weights": weights_art.path,
                             "contained_thr": 0})

    held_out_metrics_art = None
    for view, pairs in views:
        out_dir = data_dir / "artifacts" / run_label / view
        print(f"Evaluating {weights_art.key} v{ver} on {view} ({len(pairs)} images)")

        mlflow.set_experiment(experiment)
        # Unique run name per view, champion version and time.
        run_name = f"{run_label}/{view}_v{ver}_{datetime.now():%Y%m%d_%H%M%S}"
        with mlflow.start_run(run_name=run_name) as ml_run:
            mlflow.set_tag("eval_mode", view)
            mlflow.log_params({
                "weights_uid": weights_art.uid, "weights_key": weights_art.key,
                "weights_version": ver, "split": view, "framework": framework,
                "imgsz": eval_config["imgsz"], "conf": eval_config["conf"],
                "n_images": len(pairs),
            })
            collections = dataset.collections or {}
            scored = collections if (view == "all" or co) else {view: collections[split]}
            log_collection_inputs(scored)
            summary, rows = score_pairs(
                detector, [(p.image.path, p.label.path) for p in pairs],
                categories, imgsz=eval_config["imgsz"], conf=eval_config["conf"],
            )
            print_summary(summary, f"{weights_art.key} v{ver} [{view}]")
            files = write_eval_outputs(summary, rows, categories, out_dir, eval_config["worst_n"])
            for mk, mv in flatten_for_mlflow(summary).items():
                mlflow.log_metric(mk, mv)
            for f in files:
                mlflow.log_artifact(str(f))
            ml_run_id, ml_run_name = ml_run.info.run_id, ml_run.info.run_name

        # Register metrics.json and the contact sheet under the eval key for this version and view.
        metrics_key = outputs["metrics"]["key"].format(version=ver, view=view)
        metrics_dst = storage_root / metrics_key
        metrics_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_dir / "metrics.json", metrics_dst)
        metrics_art = ln.Artifact(
            metrics_dst, key=metrics_key, storage=output_storage,
            description=f"{outputs['metrics']['description']} v{ver} ({view}).",
        ).save()
        # Inputs on the metrics' own run: weights and scored data.
        metrics_art.run.input_artifacts.add(weights_art)
        if dataset.categories_artifact is not None:
            metrics_art.run.input_artifacts.add(dataset.categories_artifact)
        for pair in pairs:
            metrics_art.run.input_artifacts.add(pair.image)
            metrics_art.run.input_artifacts.add(pair.label)

        sheet = out_dir / "worst_predictions.png"
        if sheet.exists():
            sheet_key = outputs["contact_sheet"]["key"].format(version=ver, view=view)
            sheet_dst = storage_root / sheet_key
            sheet_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sheet, sheet_dst)
            ln.Artifact(sheet_dst, key=sheet_key, storage=output_storage,
                        description=f"{outputs['contact_sheet']['description']} v{ver} ({view}).").save()

        if ml_run_id:
            metrics_art.run.features.set_values(
                {feature_names.MLFLOW_RUN_ID: ml_run_id, feature_names.MLFLOW_RUN_NAME: ml_run_name}
            )
        print(f"  [{view}] eval metrics uid={metrics_art.uid}  mlflow={ml_run_id} ({ml_run_name})")
        if view == eval_label:
            held_out_metrics_art = metrics_art

    return held_out_metrics_art
