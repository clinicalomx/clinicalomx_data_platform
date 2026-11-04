"""Environment pointers: record and find a versioned Apptainer image (``.sif``).

A compute environment is a Docker image in a registry plus a ``.sif`` registered as
``ln.Artifact(kind="environment")``. :class:`EnvImage` is one build's identity: the tag a
deployment names plus the digest CI pushed. ``python -m clinicalomx.environments register`` turns
CI's manifests into Artifacts at release time; it writes to the instance.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# `import lamindb` connects to the instance, so it is imported inside each function.
if TYPE_CHECKING:  # pragma: no cover
    import lamindb as ln

from clinicalomx.features import upsert_values

# Reuse the generic, namespaced ULabel alias machinery from the model layer.
from clinicalomx.models import _alias_label

# Feature names stamped on the .sif Artifact.
F_REGISTERED_ENV = "registered_env"   # str: e.g. "croi-core-inference"
F_DOCKER_REF = "docker_ref"           # str: ghcr.io/<owner>/<img>:<tag> (build source)
F_DEF_TEXT = "def_text"               # str: the apptainer .def used to build (provenance)
# Build provenance. `docker_ref` is the tag form; the digest is a separate feature.
F_IMAGE_DIGEST = "image_digest"       # str: sha256:<64 hex>, what the tag pointed at when CI pushed
F_SOURCE_REF = "source_ref"           # str: the release or tag the build ran from
F_SIF_ORAS_REF = "sif_oras_ref"       # str: oras://... where CI published the finished .sif

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)$"
)
# Tags CI moves, so they identify nothing.
FLOATING_TAGS = frozenset({"latest", "main", "master"})

_MANIFEST_FIELDS = ("image", "digest", "source_ref", "sif_oras_ref")


def sif_key_for_image(image: str) -> str:
    """``environments/<owner>/<name>_<tag>.sif`` for a tagged registry reference.

    Matches the HPC worker's key-to-image inference.
    """
    m = _IMAGE_RE.match(image)
    if m is None:
        raise ValueError(f"not a tagged registry reference: {image!r}")
    parts = m["repository"].split("/")
    if len(parts) < 2:
        raise ValueError(f"no owner in {image!r}: the SIF key is <owner>/<name>_<tag>")
    owner, name = parts[-2:]
    return f"environments/{owner}/{name}_{m['tag']}.sif"


@dataclass(frozen=True)
class EnvImage:
    """The citable identity of one deployed container image: ``image`` (tag) and ``digest``.

    Both are required; every other field is derived.
    """

    image: str
    digest: str
    source_ref: str
    sif_oras_ref: str | None = None

    def __post_init__(self) -> None:
        m = _IMAGE_RE.match(self.image or "")
        if m is None:
            raise ValueError(
                f"image must be <repository>:<tag> as the deployment spells it, got {self.image!r}"
            )
        if m["tag"] in FLOATING_TAGS:
            raise ValueError(
                f"refusing the floating tag {m['tag']!r} in {self.image!r}: CI moves it, so a "
                "record naming it proves nothing about what ran. Use the immutable tag."
            )
        if not _DIGEST_RE.match(self.digest or ""):
            raise ValueError(
                "digest must be sha256:<64 hex> as the build reported it, got "
                f"{self.digest!r}; a tag on its own is not an identity"
            )
        if not (self.source_ref or "").strip():
            raise ValueError("source_ref is required: the release or tag the build ran from")
        if self.sif_oras_ref is not None and not self.sif_oras_ref.strip():
            raise ValueError("sif_oras_ref must be a reference or omitted, never empty")

    @property
    def repository(self) -> str:
        return _IMAGE_RE.match(self.image)["repository"]

    @property
    def tag(self) -> str:
        return _IMAGE_RE.match(self.image)["tag"]

    @property
    def registered_env(self) -> str:
        return self.repository.rsplit("/", 1)[-1]

    @property
    def sif_key(self) -> str:
        return sif_key_for_image(self.image)

    @property
    def pinned_ref(self) -> str:
        """Repository at digest, with no tag."""
        return f"{self.repository}@{self.digest}"

    @classmethod
    def from_manifest(cls, path: str | Path) -> EnvImage:
        """Read a manifest an image build wrote. Raises on anything incomplete."""
        path = Path(path)
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path}: manifest must be a JSON object")
        unknown = sorted(set(data) - set(_MANIFEST_FIELDS))
        if unknown:
            raise ValueError(f"{path}: unknown manifest keys {unknown}")
        missing = sorted({"image", "digest", "source_ref"} - set(data))
        if missing:
            raise ValueError(f"{path}: manifest is missing {missing}")
        return cls(**data)

    def manifest(self) -> dict[str, str]:
        """The manifest form, omitting an absent SIF reference."""
        return {f: getattr(self, f) for f in _MANIFEST_FIELDS if getattr(self, f) is not None}

    def provenance_features(self) -> dict[str, str | None]:
        return {
            F_IMAGE_DIGEST: self.digest,
            F_SOURCE_REF: self.source_ref,
            F_SIF_ORAS_REF: self.sif_oras_ref,
        }


def _stamp(artifact: ln.Artifact, values: dict) -> None:
    """Set single-valued str features on an artifact, per key, without destroying existing ones."""
    import lamindb as ln
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        return
    for name in values:
        if ln.Feature.filter(name=name).one_or_none() is None:
            ln.Feature(name=name, dtype=str).save()
    upsert_values(artifact.features, values)


# Registration.
def register_environment(
    sif_path: str | Path,
    *,
    key: str | None = None,
    registered_env: str | None = None,
    docker_ref: str | None = None,
    def_text: str | None = None,
    storage=None,
    image: EnvImage | None = None,
) -> ln.Artifact:
    """Register a ``.sif`` as a lamindb Artifact (kind="environment"). Idempotent by key.

    ``key`` defaults to ``environments/<owner>/<filename>``, the owner being the ``.sif``'s
    directory. ``image`` records the full build identity and supplies ``key``,
    ``registered_env`` and ``docker_ref``.
    """
    import lamindb as ln
    sif_path = Path(sif_path).resolve()
    if not sif_path.exists():
        raise FileNotFoundError(sif_path)
    if image is not None:
        key = key or image.sif_key
        registered_env = registered_env or image.registered_env
        docker_ref = docker_ref or image.image
    key = key or f"environments/{sif_path.parent.name}/{sif_path.name}"
    art = ln.Artifact.filter(key=key, is_latest=True).one_or_none()
    if art is None:
        kw = {"storage": storage} if storage is not None else {}
        art = ln.Artifact(
            sif_path, key=key, kind="environment",
            description=f"Apptainer image: {sif_path.name}", **kw,
        ).save()
    values = {F_REGISTERED_ENV: registered_env, F_DOCKER_REF: docker_ref, F_DEF_TEXT: def_text}
    if image is not None:
        values |= image.provenance_features()
    _stamp(art, values)
    return art


def link_apptainer_to_run() -> ln.Artifact | None:
    """Inside Apptainer, add the run's environment Artifact to its inputs. Never registers.

    Returns ``None`` when ``APPTAINER_CONTAINER`` is unset or the ``.sif`` is unregistered. Call
    once after the run starts.
    """
    import lamindb as ln
    sif = os.environ.get("APPTAINER_CONTAINER")
    if not sif:
        return None
    key = f"environments/{Path(sif).parent.name}/{Path(sif).name}"
    art = ln.Artifact.filter(key=key, kind="environment", is_latest=True).one_or_none()
    if art is None:
        print(f"link_apptainer_to_run: no environment Artifact at {key!r}; this run cites none",
              file=sys.stderr)
        return None
    ln.context.run.input_artifacts.add(art)
    return art


def _env_alias_ns(registered_env: str, alias_namespace: str | None = None) -> str:
    """Alias namespace for an environment, one per ``registered_env``."""
    return alias_namespace or f"env_alias:{registered_env}"


def promote_environment(
    alias: str, registered_env: str, alias_namespace: str | None = None, *,
    lamin_uid: str | None = None, lamin_key: str | None = None,
) -> ln.Artifact:
    """Point ``alias`` (such as 'production') at an environment Artifact, per ``registered_env``."""
    import lamindb as ln
    if not (lamin_uid or lamin_key):
        raise ValueError("pass lamin_uid or lamin_key")
    art = ln.Artifact.get(uid=lamin_uid) if lamin_uid else ln.Artifact.get(key=lamin_key)
    if art.kind != "environment":
        raise ValueError(f"artifact {art.uid} is kind={art.kind!r}, expected 'environment'")
    label = _alias_label(alias, _env_alias_ns(registered_env, alias_namespace), create=True)
    for prev in ln.Artifact.filter(ulabels=label):
        if prev.uid != art.uid:
            prev.ulabels.remove(label)
    art.ulabels.add(label)
    print(f"env alias {alias!r} -> {art.key} (uid={art.uid})")
    return art


def plan_registrations(
    storage_root: str | Path, manifest: str | Path
) -> list[tuple[EnvImage, Path]]:
    """Pair each build manifest with the ``.sif`` the HPC worker resolves for it.

    ``manifest`` is a JSON file or a directory of them. Each ``.sif`` must already sit at
    ``<storage_root>/<sif_key>``. Raises on any bad manifest, so nothing is written.
    """
    manifest = Path(manifest)
    paths = sorted(manifest.glob("*.json")) if manifest.is_dir() else [manifest]
    if not paths:
        raise ValueError(f"no manifests found in {manifest}")
    root = Path(storage_root)
    planned = []
    for path in paths:
        image = EnvImage.from_manifest(path)
        sif = root / image.sif_key
        if not sif.exists():
            how = (f"apptainer pull {sif} {image.sif_oras_ref}" if image.sif_oras_ref
                   else f"apptainer build {sif} docker://{image.pinned_ref}")
            raise FileNotFoundError(f"{sif} is missing; materialise it first:\n  {how}")
        planned.append((image, sif))
    return planned


def _storage_for_root(root: str | Path):
    """The registered lamindb Storage whose root is ``root``, else a ValueError naming the
    roots that exist."""
    import lamindb as ln
    want = str(Path(root))
    for storage in ln.Storage.filter():
        if str(storage.root).rstrip("/") == want:
            return storage
    have = ", ".join(sorted(str(s.root) for s in ln.Storage.filter())) or "(none)"
    raise ValueError(f"{want} is not a registered storage root. Registered: {have}")


def _register(argv) -> int:
    # Plan every manifest before writing anything: all or nothing.
    planned = [p for m in argv.manifest for p in plan_registrations(argv.storage_root, m)]
    storage = _storage_for_root(argv.storage_root)
    for image, sif in planned:
        art = register_environment(sif, image=image, storage=storage)
        if argv.alias:
            promote_environment(argv.alias, image.registered_env, lamin_uid=art.uid)
        print("\t".join([art.uid, art.key or "", image.pinned_ref, image.source_ref,
                         str(art.path)]))
    return 0


def _main(argv=None) -> int:
    """Release-time CLI that records what a build produced. Writes to the instance::

        python -m clinicalomx.environments register \\
            --storage-root "$CLINICALOMX_SITE_HPC_ROOT" --manifest env-manifests/
    """
    p = argparse.ArgumentParser(prog="clinicalomx.environments")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="record built images as kind=environment Artifacts")
    r.add_argument("--storage-root", required=True,
                   help="the lamindb storage root the SIFs are cached under")
    r.add_argument("--manifest", required=True, action="append", metavar="PATH",
                   help="an env manifest JSON written by an image build, or a directory of them; "
                        "repeatable")
    r.add_argument("--alias", help="also point this per-env alias at each result")
    args = p.parse_args(argv)
    if args.cmd == "register":
        return _register(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
