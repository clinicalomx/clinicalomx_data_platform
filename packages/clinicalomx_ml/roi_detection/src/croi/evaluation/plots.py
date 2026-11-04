"""Evaluation plots logged to MLflow.

  - recall_by_modality:     is there an mIF vs H&E gap?
  - iou_distribution:       are matched boxes tight?
  - per_image_iou_scatter:  which images are hardest?
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


IOU_THRESHOLDS = (0.5, 0.7, 0.9)
MODALITIES = ("mIF", "H&E")


def recall_by_modality(summary: dict, out_path: Path) -> Path:
    """Grouped bar chart: recall@{0.5,0.7,0.9} by modality, plus overall."""
    groups = ["overall"] + [f"modality:{m}" for m in MODALITIES]
    labels = ["overall"] + list(MODALITIES)

    x = np.arange(len(groups))
    width = 0.25

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, thr in enumerate(IOU_THRESHOLDS):
        vals = [summary[g].get(f"recall@{thr}", float("nan")) for g in groups]
        ax.bar(x + (i - 1) * width, vals, width, label=f"IoU>={thr}")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.0)
    ax.set_ylabel("recall")
    ax.set_title("Recall by modality across IoU thresholds")
    ax.legend(loc="lower right")
    ax.grid(axis="y", alpha=0.3)

    for i, thr in enumerate(IOU_THRESHOLDS):
        vals = [summary[g].get(f"recall@{thr}", float("nan")) for g in groups]
        for j, v in enumerate(vals):
            if not np.isnan(v):
                ax.text(
                    x[j] + (i - 1) * width, v + 0.02, f"{v:.2f}",
                    ha="center", va="bottom", fontsize=8,
                )

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def iou_distribution(rows: list[dict], class_names: dict[int, str], out_path: Path) -> Path:
    """Histogram of matched IoU (at IoU >= 0.5) per class."""
    by_class: dict[int, list[float]] = {c: [] for c in class_names}
    for r in rows:
        for cls, _box in r["gts_xyxy"]:
            if cls not in by_class:
                by_class[cls] = []
        for v, cls in zip(r["matched_ious_05"], r.get("matched_classes_05", [])):
            by_class.setdefault(cls, []).append(v)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    bins = np.linspace(0.5, 1.0, 21)
    for cls, vals in by_class.items():
        if not vals:
            continue
        ax.hist(
            vals, bins=bins, alpha=0.55, label=class_names.get(cls, str(cls)),
            edgecolor="black", linewidth=0.5,
        )
    ax.axvline(0.7, color="orange", linestyle="--", linewidth=1, label="IoU=0.7")
    ax.axvline(0.9, color="red", linestyle="--", linewidth=1, label="IoU=0.9")
    ax.set_xlabel("IoU of matched predictions")
    ax.set_ylabel("count")
    ax.set_title("Matched-prediction IoU distribution by class")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def per_image_iou_scatter(rows: list[dict], out_path: Path) -> Path:
    """Per-image scatter: x = number of GT boxes, y = worst matched IoU, colour = modality."""
    color_map = {"mIF": "#1f77b4", "H&E": "#d62728"}

    fig, ax = plt.subplots(figsize=(8, 5))
    for mod, color in color_map.items():
        xs, ys = [], []
        for r in rows:
            if r["modality"] != mod:
                continue
            n_gt = len(r["gts_xyxy"])
            min_iou = min(r["matched_ious_05"]) if r["matched_ious_05"] else 0.0
            xs.append(n_gt + np.random.uniform(-0.1, 0.1))  # jitter
            ys.append(min_iou)
        ax.scatter(xs, ys, c=color, label=mod, alpha=0.6, s=30, edgecolors="white", linewidths=0.5)

    ax.axhline(0.7, color="orange", linestyle="--", linewidth=1)
    ax.axhline(0.9, color="red", linestyle="--", linewidth=1)
    ax.set_xlabel("# ground-truth boxes in image")
    ax.set_ylabel("worst matched IoU (0 if any GT missed)")
    ax.set_title("Per-image worst-IoU by modality")
    ax.legend()
    ax.grid(alpha=0.3)
    ax.set_ylim(-0.02, 1.02)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
