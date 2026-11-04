"""Build the clinical_roi apptainer ``.sif`` per objective and mode, register it as a lamindb
environment Artifact, and promote an alias to it.

For local experiments only: records carry the image tag, not a digest. Release-time registration
is ``python -m clinicalomx.environments register --manifest ...``. Needs ``apptainer``, registry
credentials (``APPTAINER_DOCKER_USERNAME``/``APPTAINER_DOCKER_PASSWORD``) and the instance, so
run on the HPC after ``bash containers/build.sh`` has pushed the images.

Usage:
  uv run python containers/register_environments.py \
      --registry ghcr.io/<owner> --tag v0.1.0 \
      --objectives general core --modes inference train --alias production
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import lamindb as ln

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# Import clinicalomx and croi from the sibling source trees.
for _p in (REPO / "src", REPO.parent / "clinicalomx/src"):
    if _p.is_dir():
        sys.path.insert(0, str(_p))

import clinicalomx as cx  # noqa: E402



def _connect_storage():
    """Connect lamindb through the general manifest and return its Storage for .sif artifacts."""
    from croi.objectives.tissue_objects import environment  # noqa: E402

    manifest = environment.load_manifest(environment.resolve_config("manifest.yaml"))
    if not manifest.is_accessible():
        raise EnvironmentError("lamindb/storage endpoint not accessible from this host")
    manifest.connect()
    return manifest.storage.provision()


def create_env(objective: str, mode: str, *, tag: str, registry: str, alias: str,
               storage, def_dir: Path, sif_dir: Path) -> "ln.Artifact":
    name = f"croi-{objective}-{mode}"
    docker_ref = f"{registry}/{name}:{tag}"
    def_path = def_dir / f"{name}.def"
    key = cx.sif_key_for_image(docker_ref)
    sif_path = sif_dir / Path(key).relative_to("environments")

    def_path.write_text(f"Bootstrap: docker\nFrom: {docker_ref}\n")
    if not sif_path.exists():
        print(f">> apptainer build {sif_path}  <-  {docker_ref}")
        sif_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["apptainer", "build", str(sif_path), str(def_path)], check=True)
    else:
        print(f">> reuse existing {sif_path}")

    art = cx.register_environment(
        sif_path, key=key, registered_env=name, docker_ref=docker_ref, storage=storage,
    )
    # One alias namespace per registered env, so promoting one never moves another's alias.
    cx.promote_environment(alias, name, lamin_uid=art.uid)
    print(f">> registered + promoted {name} -> {alias}  (uid={art.uid})\n")
    return art


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Create clinical_roi compute environments (lamindb).")
    p.add_argument("--objectives", nargs="+", default=["general", "core"])
    p.add_argument("--modes", nargs="+", default=["inference", "train"])
    registry = os.environ.get("CLINICALOMX_SITE_IMAGE_REGISTRY")
    p.add_argument("--registry", default=registry, required=registry is None,
                   help="default: CLINICALOMX_SITE_IMAGE_REGISTRY from site.env")
    p.add_argument("--tag", default="main", help="image tag / git ref (use a vX.Y.Z release)")
    p.add_argument("--alias", default="production")
    p.add_argument("--def-dir", default=str(HERE / "apptainer"))
    p.add_argument("--sif-dir", help="where to build .sif (default: <storage_root>/environments)")
    args = p.parse_args(argv)

    storage = _connect_storage()
    sif_dir = Path(args.sif_dir) if args.sif_dir else Path(storage.path) / "environments"
    def_dir = Path(args.def_dir)
    def_dir.mkdir(parents=True, exist_ok=True)
    sif_dir.mkdir(parents=True, exist_ok=True)

    for obj in args.objectives:
        for mode in args.modes:
            create_env(obj, mode, tag=args.tag, registry=args.registry, alias=args.alias,
                       storage=storage, def_dir=def_dir, sif_dir=sif_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
