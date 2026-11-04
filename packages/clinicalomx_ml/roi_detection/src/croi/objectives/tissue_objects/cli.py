"""The tissue_objects stages of ``python -m croi``.

``train``, ``validate`` and ``resolve-model`` are every objective's, in ``croi.cli``.
"""
from __future__ import annotations

import argparse

from croi.config import Subcommand
from croi.objectives.tissue_objects.config import (
    ExtractPyramidConfig,
    ExtractTmaCropsConfig,
    IngestConfig,
    IngestLabelsConfig,
    SplitConfig,
)


def _config(args: argparse.Namespace) -> dict:
    """The stage yaml the caller asked for, from the packaged configs dir, validated."""
    from croi.objectives.tissue_objects import environment

    return environment.load_config(args.config or args._stage.config, args._stage.schema)


def _limit_argument(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, metavar="N",
                   help="process only the first N resolved inputs (--limit 1 is a dry run)")


def _ingest(args):
    config = _config(args)
    from croi.objectives.tissue_objects import ingest

    ingest.run(config, limit=args.limit)


def _ingest_labels(args):
    config = _config(args)
    from croi.objectives.tissue_objects import labels

    labels.run(config)


def _split(args):
    config = _config(args)
    from croi.objectives.tissue_objects import split

    split.run(config)


def _extract_tma_crops(args):
    config = _config(args)
    from croi.objectives.tissue_objects import extract

    extract.run_tma_crops(config, limit=args.limit)


def _extract_pyramid(args):
    config = _config(args)
    from croi.objectives.tissue_objects import extract

    extract.run_pyramid_low_scale(config, limit=args.limit)


def _render_overlays(args):
    from croi.objectives.tissue_objects.overlay import render_detections_jsonl

    out = render_detections_jsonl(
        args.jsonl, args.out_dir,
        min_conf=args.min_conf, max_size=args.max_size,
        jpeg_quality=args.per_image_quality, per_sheet=args.per_sheet,
        cols=args.cols, cell_px=args.cell_px, skip_empty=args.skip_empty,
        per_image=not args.no_per_image, contact_sheets=not args.no_contact_sheet,
    )
    print(f"\nDone. Open: {out}/")


def _render_overlays_arguments(o: argparse.ArgumentParser) -> None:
    o.add_argument(
        "jsonl",
        help="a detections JSONL you supply - an earlier serving run on disk, or written by "
             "hand. One JSON object per line: qptiff_uid or qptiff_key, plus detections[] "
             "carrying class_id, class_name, confidence and bbox_thumb_xyxy in thumbnail "
             "pixels, all four required. Nothing in this package writes one; its writer "
             "(general/inference/serve.py) was deleted in the re-layering. "
             "See croi.objectives.tissue_objects.overlay.render_detections_jsonl")
    o.add_argument("--out-dir", default=None,
                   help="output dir (default: <jsonl_parent>/overlays/<jsonl_stem>/)")
    o.add_argument("--min-conf", type=float, default=0.0,
                   help="drop detections below this confidence before rendering")
    o.add_argument("--max-size", type=int, default=640, help="per-image max edge (px)")
    o.add_argument("--per-image-quality", type=int, default=70)
    o.add_argument("--per-sheet", type=int, default=36, help="cells per contact sheet")
    o.add_argument("--cols", type=int, default=6)
    o.add_argument("--cell-px", type=int, default=220)
    o.add_argument("--skip-empty", action="store_true",
                   help="skip QPTIFFs with no detections in per-image output")
    o.add_argument("--no-contact-sheet", action="store_true", help="only write per-image JPEGs")
    o.add_argument("--no-per-image", action="store_true", help="only write contact sheets")


SUBCOMMANDS = {
    "ingest": Subcommand("0_ingest.yaml", IngestConfig, _ingest,
                         "QPTIFF -> thumbnail PNG + WholeSlide ROI", _limit_argument),
    "ingest-labels": Subcommand("1_ingest_labels.yaml", IngestLabelsConfig, _ingest_labels,
                                "register Label Studio labels against their PNGs"),
    "split": Subcommand("2_split.yaml", SplitConfig, _split,
                        "deterministic train/val/test Collections"),
    "extract-tma-crops": Subcommand("4_extract_tma_crops.yaml", ExtractTmaCropsConfig,
                                    _extract_tma_crops, "detect TMA grids and cut the crop release",
                                    _limit_argument),
    "extract-pyramid": Subcommand("5_extract_pyramid_low_scale.yaml", ExtractPyramidConfig,
                                  _extract_pyramid, "render the low-res pyramid projection release",
                                  _limit_argument),
    "render-overlays": Subcommand(None, None, _render_overlays,
                                  "draw a caller-supplied detections JSONL as JPEGs and sheets",
                                  _render_overlays_arguments),
}
