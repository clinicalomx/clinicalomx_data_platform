"""Fetch a trained checkpoint from MLflow.

Resolution modes:
  --mlflow-run <run_id>     direct MLflow lookup
  --lamin-uid   <uid>       lamindb artifact uid; reads its `mlflow_run_id` feature

Works with proxied and direct-filesystem MLflow servers. Requires `MLFLOW_TRACKING_URI`.
Writes to `runs/serving/best.pt` by default.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import mlflow
from mlflow.exceptions import MlflowException

DEFAULT_OUT = Path("runs/serving/best.pt")
# best.pt's path in an MLflow run: pyfunc model layout first, then the older raw run dir.
CANDIDATE_ARTIFACT_PATHS = ("model/artifacts/weights/best.pt", "run/weights/best.pt")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--mlflow-run",
        help="MLflow run id whose `weights/best.pt` we want.",
    )
    src.add_argument(
        "--lamin-uid",
        help="lamindb Artifact uid for a registered best.pt; we read its "
        "mlflow_run_id feature and download from there.",
    )
    p.add_argument(
        "--artifact-path",
        default=None,
        help="MLflow artifact path inside the run. Default: try "
        + ", then ".join(CANDIDATE_ARTIFACT_PATHS) + ".",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Where to copy the fetched file on disk (default: {DEFAULT_OUT}).",
    )
    return p.parse_args()


def _mlflow_run_from_lamin(uid: str) -> str:
    """The mlflow_run_id feature of a registered best.pt artifact."""
    import lamindb as ln

    art = ln.Artifact.get(uid=uid)
    # The feature may be on the artifact or on its creating run.
    values = dict(art.features.get_values() or {})
    if "mlflow_run_id" not in values and art.run is not None:
        values.update(art.run.features.get_values() or {})
    rid = values.get("mlflow_run_id")
    if not rid:
        raise SystemExit(
            f"lamindb artifact {uid} has no mlflow_run_id feature. "
            "Either it predates the feature, or training failed to stamp it."
        )
    return rid


def main() -> int:
    args = parse_args()

    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        print(
            "ERROR: MLFLOW_TRACKING_URI not set. Source your .env first.",
            file=sys.stderr,
        )
        return 2
    mlflow.set_tracking_uri(uri)

    run_id = args.mlflow_run or _mlflow_run_from_lamin(args.lamin_uid)
    candidates = [args.artifact_path] if args.artifact_path else list(CANDIDATE_ARTIFACT_PATHS)

    # Move the download to args.out, trying each candidate layout.
    src = None
    for ap in candidates:
        try:
            dl_path = mlflow.artifacts.download_artifacts(run_id=run_id, artifact_path=ap)
        except MlflowException as e:
            print(f"  (no {ap}: {type(e).__name__})")
            continue
        if Path(dl_path).exists():
            print(f"Fetched {ap} from MLflow run {run_id}")
            src = Path(dl_path)
            break
    if src is None:
        print(f"ERROR: no best.pt under {candidates} in run {run_id}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()
    shutil.copy2(src, args.out)
    print(f"  -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
