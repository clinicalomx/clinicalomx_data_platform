"""Stamp ``model_framework`` on every registered croi model that lacks it.

``croi.io.registries.framework_of`` refuses a model without ``model_framework``.

- ``check`` is read-only. It lists every ``kind="model"`` artifact under ``models/`` without
  ``model_framework``, with a hint from its key, then a count. Exit 0 when the count is zero.
- ``apply UID=FRAMEWORK ...`` stamps exactly the models named. Hints are never applied. Already
  stamped models are left alone and reported.

Run inside the croi train image on a node that reaches the instance, as
``python -m croi.utils.backfill_model_framework`` or as a file. The instance comes from
``tma_core``'s ``manifest.yaml``.
"""
from __future__ import annotations

import argparse
import sys
from importlib import import_module

PREFIX = "models/"
HINTS = (("rf-detr", "rfdetr"), ("rfdetr", "rfdetr"), ("yolo", "ultralytics"))


def hint(key: str) -> str:
    """The framework the key's arch suggests, or ``?``. Printed, never applied."""
    name = key.rsplit("/", 1)[-1].lower()
    return next((framework for marker, framework in HINTS if marker in name), "?")


def unstamped() -> list:
    """Every registered croi model with no ``model_framework`` value, oldest first."""
    import lamindb as ln

    from croi import feature_names

    models = ln.Artifact.filter(kind="model", key__startswith=PREFIX).order_by("created_at")
    return [art for art in models
            if not (art.features.get_values() or {}).get(feature_names.MODEL_FRAMEWORK)]


def check() -> int:
    missing = unstamped()
    for art in missing:
        print(f"  {art.uid}  {art.key}  version={art.version}  hint={hint(art.key)}")
    print(f"{len(missing)} registered model(s) record no model_framework")
    return 1 if missing else 0


def parse(pairs: list[str]) -> dict[str, str]:
    """Parse ``UID=FRAMEWORK`` pairs; each framework must have a detector."""
    from croi.detection.backends import DETECTORS

    stamps: dict[str, str] = {}
    for pair in pairs:
        uid, sep, framework = pair.partition("=")
        if not sep or not uid or framework not in DETECTORS:
            raise SystemExit(f"{pair!r}: expected UID=FRAMEWORK, FRAMEWORK one of {sorted(DETECTORS)}")
        stamps[uid] = framework
    return stamps


def apply(stamps: dict[str, str]) -> None:
    import lamindb as ln

    from croi import feature_names

    for uid, framework in stamps.items():
        art = ln.Artifact.get(uid=uid)
        recorded = (art.features.get_values() or {}).get(feature_names.MODEL_FRAMEWORK)
        if recorded:
            print(f"  SKIP {art.key}: already records {recorded}")
            continue
        # add_values, not `croi.train.stamp_framework`: older images call `set_values`, which
        # erases the MLflow coordinates.
        art.features.add_values({feature_names.MODEL_FRAMEWORK: framework})
        print(f"  STAMPED {art.key}: {framework}")


def connect() -> None:
    environment = import_module("croi.objectives.tma_core.environment")
    manifest = environment.load_manifest(environment.resolve_config("manifest.yaml"))
    manifest.connect()
    print(f"instance {manifest.instance}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("stamps", nargs="*", metavar="UID=FRAMEWORK")
    args = parser.parse_args(argv)
    if (args.command == "apply") != bool(args.stamps):
        parser.error("`apply` takes one or more UID=FRAMEWORK; `check` takes none")

    stamps = parse(args.stamps)
    connect()
    if args.command == "check":
        return check()
    apply(stamps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
