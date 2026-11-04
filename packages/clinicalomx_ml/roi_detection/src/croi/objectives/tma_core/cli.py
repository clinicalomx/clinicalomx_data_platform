"""The tma_core stages of ``python -m croi``.

There is no ``ingest`` stage: tma_core trains on the substrate tissue_objects cuts
(``python -m croi tissue_objects extract-tma-crops`` / ``extract-pyramid``). ``train``,
``validate`` and ``resolve-model`` are every objective's, in ``croi.cli``.
"""
from __future__ import annotations

import argparse

from croi.config import Subcommand
from croi.objectives.tma_core.config import (
    ExtractTmaCoresConfig,
    IngestLabelsConfig,
    SplitConfig,
)


def _config(args: argparse.Namespace) -> dict:
    """The stage yaml the caller asked for, from the packaged configs dir, validated."""
    from croi.objectives.tma_core import environment

    return environment.load_config(args.config or args._stage.config, args._stage.schema)


def _ingest_labels(args):
    config = _config(args)
    from croi.objectives.tma_core import labels

    labels.run(config)


def _split(args):
    config = _config(args)
    from croi.objectives.tma_core import split

    split.run(config)


def _extract_tma_cores(args):
    config = _config(args)
    from croi.objectives.tma_core import extract

    extract.run_tma_cores(config, image=args.image, limit=args.limit)


def _extract_tma_cores_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--image", metavar="KEY", help="dearray exactly this one qptiff artifact key")
    p.add_argument("--limit", type=int, metavar="N",
                   help="process only the first N resolved inputs (--limit 1 is a dry run)")


SUBCOMMANDS = {
    "ingest-labels": Subcommand("1_ingest_labels.yaml", IngestLabelsConfig, _ingest_labels,
                                "register Label Studio core labels against their source images"),
    "split": Subcommand("2_split.yaml", SplitConfig, _split,
                        "deterministic, slide-stratified train/val/test Collections"),
    "extract-tma-cores": Subcommand("4_extract_tma_cores.yaml", ExtractTmaCoresConfig,
                                    _extract_tma_cores,
                                    "dearray each QPTIFF into TMA + Core shapes and the ROI graph",
                                    _extract_tma_cores_arguments),
}
