"""One package per detection framework, and `detector_for`, which picks between them.

`DETECTORS` maps a framework name to its `ObjectDetector`; `training.TRAINERS` maps the same names
to their `Trainer`. Each `<framework>/` keeps `detector` and `trainer` apart so the production
detector never imports mlflow. `ensemble` fuses several detectors into one. Importing this module
imports no framework.

    model:
      framework: ultralytics         # which backend class; the keys of DETECTORS
      weights: production            # alias | path | {lamin_uid|lamin_key}
      tta: true                      # ultralytics augmented inference
      imgsz: [640, 960, 1280]        # multi-scale; predictions fused
      ensemble:                      # several checkpoints, predictions fused
        - models/run_a/best.pt
        - {alias: challenger}
      fuse_iou: 0.6                  # IoU threshold for cross-pass fusion
"""

from __future__ import annotations

from croi.config import DEFAULT_FRAMEWORK, Framework
from croi.detection.backends import ensemble, rfdetr, ultralytics
from croi.detection.detector import ObjectDetector, ResolveAlias

#: `ObjectDetector` classes by framework, constructed as
#: `cls(weights, *, resolve_alias, tta, imgsz, fuse_iou, contained_thr, contained_agnostic, extra)`.
#: `cls.DEFAULT_WEIGHTS` applies when no weights are named. Same keys as `training.TRAINERS`.
DETECTORS: dict[Framework, type[ObjectDetector]] = {
    "ultralytics": ultralytics.Detector,
    "rfdetr": rfdetr.Detector,
}

__all__ = ["DEFAULT_FRAMEWORK", "DETECTORS", "detector_for"]


def detector_for(cfg: dict | None, *, resolve_alias: ResolveAlias | None = None) -> ObjectDetector:
    """Build an `ObjectDetector` from a `model:` config block, or None for the defaults.

    Keys: framework, weights, tta, imgsz, fuse_iou, contained_thr, contained_agnostic, ensemble,
    extra. ``framework`` defaults to `croi.config.DEFAULT_FRAMEWORK`. With ``ensemble``, returns an
    `ensemble.Detector` over ``[weights] + ensemble`` in one framework. A backend with no
    ``DEFAULT_WEIGHTS`` refuses a block without weights. ``resolve_alias`` reads alias specs.
    """
    cfg = dict(cfg or {})
    framework = str(cfg.get("framework", DEFAULT_FRAMEWORK))
    if framework not in DETECTORS:
        raise ValueError(
            f"unknown framework {framework!r}; declared detectors: {sorted(DETECTORS)}"
        )
    backend = DETECTORS[framework]
    # A backend may be any callable returning an `ObjectDetector`.
    weights = cfg["weights"] if "weights" in cfg else getattr(backend, "DEFAULT_WEIGHTS", None)
    if weights is None:
        raise ValueError(f"the {framework} backend needs `weights`; it has no default checkpoint")
    tta = bool(cfg.get("tta", False))
    imgsz = cfg.get("imgsz", 640)
    fuse_iou = float(cfg.get("fuse_iou", 0.6))
    # Drop a box at least `contained_thr` inside another; 0 disables.
    contained_thr = float(cfg.get("contained_thr", 0.9))
    contained_agnostic = bool(cfg.get("contained_agnostic", False))
    extra = cfg.get("extra")

    kw = {"resolve_alias": resolve_alias, "tta": tta, "imgsz": imgsz, "fuse_iou": fuse_iou,
          "contained_thr": contained_thr, "contained_agnostic": contained_agnostic, "extra": extra}
    base = backend(weights, **kw)
    ensemble_weights = cfg.get("ensemble")
    if not ensemble_weights:
        return base
    others = [backend(w, **kw) for w in ensemble_weights]
    return ensemble.Detector(
        [base, *others], fuse_iou=fuse_iou,
        contained_thr=contained_thr, contained_agnostic=contained_agnostic,
    )
