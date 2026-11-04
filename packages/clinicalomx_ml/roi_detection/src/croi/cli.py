"""ONE entry point for every stage of every objective: ``python -m croi <objective> <stage>``.

Each objective's ``cli`` module declares its stages in ``SUBCOMMANDS``; this module adds
``train``, ``validate`` and ``resolve-model``. ``--config`` takes a stage yaml name in the
objective's packaged configs dir, or a path.
"""
from __future__ import annotations

import argparse
from importlib import import_module
from types import ModuleType

from croi import objectives
from croi.config import Subcommand, TrainConfig
from croi.objectives import NAMES


def _environment(args: argparse.Namespace) -> ModuleType:
    return import_module(f"croi.objectives.{args.objective}.environment")


def _config(args: argparse.Namespace) -> dict:
    """The validated stage yaml from ``--config`` or the stage default."""
    return _environment(args).load_config(args.config or args._stage.config, args._stage.schema)


def _train(args: argparse.Namespace) -> None:
    config = _config(args)
    from croi import train

    train.run(args.objective, _environment(args).preflight(roi_schema=False), config,
              eval_only=args.eval_only, weights=args.weights)


def _train_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--eval-only", action="store_true",
                   help="skip training; evaluate an already-registered champion instead")
    p.add_argument("--weights", default=None,
                   help="with --eval-only: uid:<uid> | version:<n>; default = the production alias")


def _validate(args: argparse.Namespace) -> None:
    """Print `DetectionDataset.validate` findings without training."""
    config = _config(args)
    _environment(args).preflight(roi_schema=False)
    dataset = objectives.get(args.objective).load_dataset(config)
    print(f"{dataset.name}: {dataset.describe()}")
    findings = dataset.validate()
    for finding in findings:
        print(f"  {finding}")
    print(f"\n{len(findings)} finding(s).")


def _resolve_model(args: argparse.Namespace) -> None:
    """Resolve the served champion from either registry and print it."""
    _environment(args).preflight(roi_schema=False, mlflow_env=True)
    ref = objectives.get(args.objective).registry.model_ref({
        "uid": args.uid, "key": args.key, "alias": args.alias,
        "mlflow_version": args.mlflow_version, "mlflow_alias": args.mlflow_alias,
        "mlflow_run_id": args.mlflow_run_id,
    })
    print(ref)
    print(f"  weights (best.pt): {ref.path}")
    print(f"  mlflow model_uri : {ref.model_uri}")


def _resolve_model_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--uid", help="lamindb Artifact uid")
    g.add_argument("--key", help="lamindb Artifact key (e.g. models/<objective>/best.pt)")
    g.add_argument("--alias", help="alias on either registry (default: production)")
    g.add_argument("--mlflow-version", help="MLflow registered-model version")
    g.add_argument("--mlflow-alias", help="MLflow registered-model alias")
    g.add_argument("--mlflow-run-id", help="MLflow run id")


#: The stages every objective has.
SHARED_SUBCOMMANDS = {
    "train": Subcommand("3_train_and_eval.yaml", TrainConfig, _train,
                        "fine-tune, register + evaluate a champion", _train_arguments),
    "validate": Subcommand("3_train_and_eval.yaml", TrainConfig, _validate,
                           "report dataset findings without training"),
    "resolve-model": Subcommand(None, None, _resolve_model,
                                "resolve the served champion and print it",
                                _resolve_model_arguments),
}


def subcommands(objective: str) -> dict[str, Subcommand]:
    """Every stage of ``objective``: its own, then the shared ones, which none may shadow."""
    own = import_module(f"croi.objectives.{objective}.cli").SUBCOMMANDS
    clash = sorted(set(own) & set(SHARED_SUBCOMMANDS))
    if clash:
        raise ValueError(f"{objective} redefines the shared stages {clash}")
    return {**own, **SHARED_SUBCOMMANDS}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m croi", description=__doc__.splitlines()[0])
    objective_parsers = p.add_subparsers(dest="objective", required=True)
    for name in NAMES:
        module = import_module(f"croi.objectives.{name}.cli")
        stages = objective_parsers.add_parser(name, help=module.__doc__.splitlines()[0])
        sub = stages.add_subparsers(dest="stage", required=True)
        for stage_name, stage in subcommands(name).items():
            s = sub.add_parser(stage_name, help=stage.help)
            s.set_defaults(_run=stage.run, _stage=stage)
            if stage.config:
                s.add_argument("--config", default=None,
                               help=f"stage config yaml name or path (default: {stage.config})")
            if stage.arguments:
                stage.arguments(s)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args._run(args)
