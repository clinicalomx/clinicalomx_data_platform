"""build-environments: download every declared HPC SIF environment once, before compute needs it.

Each SIF is converted in CI and published as an ORAS artifact. This flow downloads the published
blob over HTTPS; it needs no ``apptainer``. An environment with no published SIF is refused.

Safety: ``concurrency_limit: 1`` on the deployment, a non-blocking ``flock`` on ``<target>.lock``,
a temp file promoted with ``os.replace``, and the layer's sha256 checked before promotion. A
manifest whose tag differs from the declared image tag raises :class:`StaleManifest` before any
download.

Touches no database. Registration stays with ``python -m clinicalomx.environments register``,
which this flow logs for the operator. Run it once per release, after the ``image:`` tags move.
"""
from __future__ import annotations

import os
import re
import sys as _sys
import time
from dataclasses import dataclass
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

#: The two deployment declarations, resolved from this file, not the CWD.
_PKG_ROOT = _P(__file__).resolve().parents[3]
_PACKAGES = _P(__file__).resolve().parents[5]
PREFECT_YAMLS = (
    _PKG_ROOT / "prefect.yaml",
    _PACKAGES / "clinicalomx_ml" / "roi_detection" / "prefect.yaml",
)

#: ``sha256:<64 hex>``. Checked here, because the manifest is written by another system.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Registry credentials, from the deployment's ``registry_auth_block``. The hpc worker exports them
#: under these names. Never write their values to a log, a report or a file.
REGISTRY_USER_ENV = "APPTAINER_DOCKER_USERNAME"
REGISTRY_PASS_ENV = "APPTAINER_DOCKER_PASSWORD"

#: Pauses between attempts at one blob transfer. Each retry resumes, keeping what it received.
FETCH_RETRY_DELAYS = (10.0, 30.0, 60.0, 120.0)

#: Both media types: a registry may serve either for the same artifact.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


@dataclass(frozen=True)
class Declared:
    """One environment, as the deployments declare it.

    ``sif_key`` is the ``sif_artifact`` value as ``prefect deploy`` renders it.
    """

    name: str                       # the registered_env: the repository basename of ``image``
    image: str                      # ghcr.io/<owner>/<name>:<tag>, the tag a deployment runs
    sif_key: str                    # environments/<owner>/<name>_<tag>.sif
    declared_by: tuple[str, ...]    # the deployments that name it


@dataclass(frozen=True)
class OrasRef:
    """A parsed ``oras://<registry>/<repository>:<tag>`` reference."""

    registry: str
    repository: str
    tag: str


class StaleManifest(ValueError):
    """The manifest on scratch describes a version the deployments no longer declare."""


#: A site value in a prefect.yaml string, ``{{ $NAME }}``, filled from the environment.
_SITE_VALUE = re.compile(r"\{\{\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _render(value: str) -> str:
    """``value`` with each ``{{ $NAME }}`` filled from the environment, as the deployment has it."""
    def fill(m: re.Match) -> str:
        if m[1] not in os.environ:
            raise ValueError(f"{m[1]} is not set, and the deployments use it in {value!r}")
        return os.environ[m[1]]
    return _SITE_VALUE.sub(fill, value)


def _job_variables(path: _P) -> list[tuple[str, dict]]:
    """``(deployment name, job_variables)`` for every deployment in one prefect.yaml."""
    import yaml

    doc = yaml.safe_load(path.read_text()) or {}
    out = []
    for dep in doc.get("deployments") or []:
        pool = dep.get("work_pool") or {}
        out.append((dep.get("name") or "?", pool.get("job_variables") or {}))
    return out


def declared_environments(paths=PREFECT_YAMLS) -> list[Declared]:
    """Every environment the deployments declare, deduplicated, in declaration order.

    Derived from each deployment's ``sif_artifact``. A missing prefect.yaml is skipped.
    """
    seen: dict[str, Declared] = {}
    for path in paths:
        if not path.exists():
            continue
        for name, jv in _job_variables(path):
            key, image = jv.get("sif_artifact"), jv.get("image")
            if not key:
                continue  # a vm-pool deployment execs no SIF
            if not image:
                raise ValueError(f"{path.name}:{name}: sif_artifact {key} with no image")
            key, image = _render(key), _render(image)
            prior = seen.get(key)
            if prior is None:
                seen[key] = Declared(
                    name=image.rsplit("/", 1)[-1].split(":")[0],
                    image=image, sif_key=key, declared_by=(name,),
                )
            elif prior.image != image:
                raise ValueError(
                    f"{key} is declared for two images: {prior.image} (by "
                    f"{', '.join(prior.declared_by)}) and {image} (by {name})"
                )
            else:
                seen[key] = Declared(prior.name, prior.image, key, (*prior.declared_by, name))
    return list(seen.values())


def read_manifest(manifest_dir, name: str) -> dict | None:
    """The build manifest for one environment, or None when the build never wrote one."""
    import json

    path = _P(manifest_dir) / f"{name}.json"
    if not path.exists():
        return None
    fix = (
        f"download the manifest again and redo the copy to {manifest_dir} "
        f"(docs/operations.md section 2.7), then run this again"
    )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: manifest is not readable JSON ({exc}): {fix}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a JSON object: {fix}")  # noqa: TRY004
    return data


def image_tag(image: str) -> str:
    """The tag of ``<registry>/<repository>:<tag>``. A registry port is not a tag."""
    repository, sep, tag = image.rpartition(":")
    if not (repository and sep and tag) or "/" in tag:
        raise ValueError(f"image must be <repository>:<tag> as the deployment spells it: {image!r}")
    return tag


def parse_oras(ref: str) -> OrasRef:
    """Split ``oras://ghcr.io/owner/name-sif:tag`` into its three parts."""
    body = ref.strip()
    if not body.startswith("oras://"):
        raise ValueError(f"not an oras reference: {ref!r}")
    body = body[len("oras://"):]
    host, _, rest = body.partition("/")
    repository, sep, tag = rest.rpartition(":")
    if not (host and repository and sep and tag) or "/" in tag:
        raise ValueError(f"oras reference must be oras://<registry>/<repository>:<tag>: {ref!r}")
    return OrasRef(registry=host, repository=repository, tag=tag)


def oras_reference(env: Declared, manifest: dict | None, manifest_dir=None) -> str:
    """The published SIF reference for one environment, or a ValueError saying why there is none.

    Checks the manifest's digest, and raises :class:`StaleManifest` when the ORAS tag differs from
    the tag the declaration asks for.
    """
    where = f" under {manifest_dir}" if manifest_dir else " under the manifest directory"
    if not manifest:
        raise ValueError(
            f"no build manifest for {env.name}{where}; "
            f"docs/operations.md section 2.7 downloads them and copies them to scratch"
        )
    wanted = image_tag(env.image)
    fix = (
        f"download the manifest of the image build for {wanted} again and redo the copy to "
        f"{manifest_dir or 'the manifest directory'} (docs/operations.md section 2.7), then run "
        f"this again"
    )
    digest = (manifest.get("digest") or "").strip()
    if digest and not DIGEST_RE.match(digest):
        raise ValueError(
            f"{env.name}: the build manifest{where} records digest {digest!r}, which is not "
            f"sha256:<64 hex>. The malformed half is {env.name}.json{where}, not the deployment "
            f"declaration: {fix}"
        )

    ref = (manifest.get("sif_oras_ref") or "").strip()
    if ref:
        try:
            parsed = parse_oras(ref)  # fail before taking the lock
        except ValueError as exc:
            raise ValueError(
                f"{env.name}: the build manifest{where} records sif_oras_ref {ref!r}, which is "
                f"not oras://<registry>/<repository>:<tag>. The malformed half is "
                f"{env.name}.json{where}, not the deployment declaration: {fix}"
            ) from exc
        if parsed.tag != wanted:
            raise StaleManifest(
                f"{env.name}: the deployments declare {env.image} and this run would write "
                f"{env.sif_key}, but the build manifest{where} resolves {ref}, which is tag "
                f"{parsed.tag!r} and not {wanted!r}. Fetching it would put {parsed.tag} bytes at "
                f"a {wanted} path, and every consumer resolves that file by path. "
                f"The manifest is the stale half: {fix}"
            )
        return ref

    # No published SIF: say first whether the manifest even describes the declared build.
    described = (manifest.get("image") or "").strip()
    if described:
        try:
            described_tag = image_tag(described)
        except ValueError as exc:
            raise ValueError(
                f"{env.name}: the build manifest{where} names image {described!r}, which is not "
                f"<repository>:<tag>. The malformed half is {env.name}.json{where}, not the "
                f"deployment declaration: {fix}"
            ) from exc
    if described and described_tag != wanted:
        raise StaleManifest(
            f"{env.name}: the deployments declare {env.image}, but the build manifest{where} "
            f"describes {described}, which is tag {described_tag!r} and not {wanted!r}. "
            f"Whether {wanted} publishes an ORAS SIF cannot be read off a manifest for another "
            f"build: {fix}"
        )
    raise ValueError(
        f"{env.name} publishes no ORAS SIF, and a docker:// conversion cannot succeed on "
        f"the HPC (DEPLOYING.md section 4). Its image build has to publish one before this "
        f"environment can be materialised here"
    )


@dataclass
class Fetched:
    """What one download produced."""

    digest: str
    size: int


def fetch_sif(ref: OrasRef, target: _P, *, timeout: float = 1800.0, transport=None) -> Fetched:
    """Download the SIF layer of an ORAS artifact to ``target``, atomically and once.

    Returns without downloading when ``target`` already holds a non-empty file. Raises
    ``BlockingIOError`` when another process holds the lock. ``transport`` is an ``httpx``
    transport.
    """
    import fcntl
    import tempfile

    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    with open(lock, "w") as handle:
        # Non-blocking. The kernel releases the lock when the process exits.
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Re-test inside the lock. An empty digest means the file was already here.
        if is_present(target):
            return Fetched(digest="", size=target.stat().st_size)

        user = os.environ.get(REGISTRY_USER_ENV) or ""
        password = os.environ.get(REGISTRY_PASS_ENV) or ""
        with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
            token = _registry_token(client, ref, user, password)
            auth = {"Authorization": f"Bearer {token}"} if token else {}
            manifest = client.get(
                f"https://{ref.registry}/v2/{ref.repository}/manifests/{ref.tag}",
                headers={**auth, "Accept": _MANIFEST_ACCEPT},
            )
            manifest.raise_for_status()
            layers = manifest.json().get("layers") or []
            if len(layers) != 1:
                raise RuntimeError(
                    f"{ref.repository}:{ref.tag} has {len(layers)} layers; an apptainer SIF "
                    f"artifact has exactly one"
                )
            layer = layers[0]
            digest, size = layer["digest"], int(layer.get("size") or 0)
            if not DIGEST_RE.match(digest):
                raise RuntimeError(f"{ref.repository}:{ref.tag} layer digest is {digest!r}")

            # Temp file in the target directory, so the promotion is a rename on one filesystem.
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".fetching.")
            tmp = _P(tmp_name)
            try:
                with os.fdopen(fd, "wb") as out:
                    sha, written = _download_blob(client, ref, digest, size, auth, out, user,
                                                  password)
                got = "sha256:" + sha.hexdigest()
                if got != digest:
                    raise RuntimeError(
                        f"{ref.repository}:{ref.tag} downloaded {written} bytes hashing to {got}, "
                        f"not the {digest} the manifest names"
                    )
                if size and written != size:
                    raise RuntimeError(
                        f"{ref.repository}:{ref.tag} downloaded {written} bytes, manifest says "
                        f"{size}"
                    )
                os.chmod(tmp, 0o644)  # mkstemp is 0600; a shared environment must be readable
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return Fetched(digest=digest, size=written)


def _download_blob(client, ref: OrasRef, digest: str, size: int, auth: dict, out, user: str,
                   password: str):
    """Stream one blob into ``out``, retrying and resuming a dropped transfer.

    Only transport failures are retried. HTTP error statuses and digest mismatches are not.
    """
    import hashlib

    import httpx

    label = f"{ref.repository}:{ref.tag}"
    url = f"https://{ref.registry}/v2/{ref.repository}/blobs/{digest}"
    attempts = len(FETCH_RETRY_DELAYS) + 1
    sha, written = hashlib.sha256(), 0
    for attempt in range(1, attempts + 1):
        if attempt > 1 and size and written >= size:
            break  # the drop came after the last byte
        try:
            if attempt > 1:
                # Refresh the registry token on each retry.
                token = _registry_token(client, ref, user, password)
                auth = {"Authorization": f"Bearer {token}"} if token else {}
            headers = {**auth, "Range": f"bytes={written}-"} if written else auth
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                if written and response.status_code != 206:
                    print(f"{label}: attempt {attempt}/{attempts}: the registry ignored a Range "
                          f"request from byte {written} (HTTP {response.status_code}), "
                          f"restarting the download from zero", flush=True)
                    out.seek(0)
                    out.truncate()
                    sha, written = hashlib.sha256(), 0
                elif written:
                    start = _range_start(response.headers.get("content-range"))
                    if start != written:
                        raise RuntimeError(
                            f"{label}: asked to resume at byte {written} and the registry "
                            f"answered Content-Range "
                            f"{response.headers.get('content-range')!r}"
                        )
                    print(f"{label}: attempt {attempt}/{attempts}: resuming at byte {written}",
                          flush=True)
                # Unbuffered, so a drop loses nothing already received.
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
        except httpx.TransportError as exc:
            failure = (f"{label}: attempt {attempt}/{attempts} failed after {written} of "
                       f"{size or 'unknown'} bytes: {type(exc).__name__}: {exc}")
            if attempt == attempts:
                print(f"{failure}; giving up", flush=True)
                raise
            delay = FETCH_RETRY_DELAYS[attempt - 1]
            print(f"{failure}; retrying in {delay:g}s", flush=True)
            time.sleep(delay)
            continue
        if attempt > 1:
            print(f"{label}: attempt {attempt}/{attempts} completed at {written} bytes",
                  flush=True)
        break
    return sha, written


def _range_start(content_range: str | None) -> int | None:
    """The first byte of ``bytes <start>-<end>/<total>``, or None when it is not that shape."""
    unit, _, span = (content_range or "").partition(" ")
    start = span.partition("-")[0]
    return int(start) if unit == "bytes" and start.isdigit() else None


def _registry_token(client, ref: OrasRef, user: str, password: str) -> str | None:
    """A pull token for one repository, or None when the registry serves it anonymously."""
    response = client.get(
        f"https://{ref.registry}/token",
        params={"service": ref.registry, "scope": f"repository:{ref.repository}:pull"},
        auth=(user, password) if password else None,
    )
    if response.status_code >= 400:
        return None  # not a token-auth registry
    return response.json().get("token") or response.json().get("access_token")


def is_present(path: _P) -> bool:
    """Whether a SIF exists at ``path`` and is non-empty."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


@flow(name="build-environments", log_prints=True)
def build_environments(
    storage_root: str,
    manifest_dir: str | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    timeout_seconds: float = 1800.0,
) -> dict:
    """Download every declared environment whose SIF is missing. No-op when all are present.

    storage_root: the lamindb storage root the worker resolves SIFs under. Targets are
        ``<storage_root>/<sif_artifact>``.
    manifest_dir: where the build manifests are (default ``<storage_root>/env-manifests``).
    only: download just these environment names. Default: every declared one.
    dry_run: report what each environment would be fetched from, and download nothing.
    timeout_seconds: per-environment HTTP timeout. A published SIF is 2 to 6GB.
    """
    logger = get_run_logger()

    root = _P(storage_root)
    manifests = _P(manifest_dir) if manifest_dir else root / "env-manifests"

    declared = declared_environments()
    if only:
        wanted = set(only)
        unknown = sorted(wanted - {e.name for e in declared})
        if unknown:
            raise ValueError(
                f"no deployment declares {unknown}; declared environments are "
                f"{sorted(e.name for e in declared)}"
            )
        declared = [e for e in declared if e.name in wanted]
    logger.info(
        f"build-environments: {len(declared)} declared environment(s) under {root} "
        f"(manifests={manifests}, dry_run={dry_run})"
    )
    if not os.environ.get(REGISTRY_PASS_ENV):
        # Not fatal: a public registry needs no credentials.
        logger.warning(
            f"{REGISTRY_PASS_ENV} is not set in this run's environment; a private registry pull "
            f"will fail. The deployment's registry_auth_block is what supplies it"
        )

    rows: list[dict] = []
    for env in declared:
        target = root / env.sif_key
        row = {
            "environment": env.name, "image": env.image, "sif_key": env.sif_key,
            "state": "", "source": "", "digest": "", "bytes": 0,
            "detail": "", "declared_by": ", ".join(env.declared_by),
        }
        rows.append(row)

        if is_present(target):
            row["state"] = "present"
            row["bytes"] = target.stat().st_size
            print(f"PRESENT      {env.name}  {env.sif_key}", flush=True)
            continue

        try:
            ref = oras_reference(env, read_manifest(manifests, env.name), manifests)
        except StaleManifest as exc:
            # A stale manifest gets its own state: the operator action differs from "unpublished".
            row["state"] = "stale"
            row["detail"] = str(exc)
            logger.error(f"{env.name}: {exc}")
            continue
        except ValueError as exc:
            row["state"] = "unpublished"
            row["detail"] = str(exc)
            logger.error(f"{env.name}: {exc}")
            continue
        row["source"] = ref

        if dry_run:
            row["state"] = "skipped"
            row["detail"] = "dry_run"
            print(f"PLAN         {env.name}  <- {ref}", flush=True)
            continue

        try:
            got = fetch_sif(parse_oras(ref), target, timeout=timeout_seconds)
        except BlockingIOError:
            row["state"] = "building"
            row["detail"] = "another builder holds the lock on this path"
            print(f"BUILDING     {env.name}  locked by another builder", flush=True)
            continue
        except Exception as exc:
            row["state"] = "failed"
            row["detail"] = f"{type(exc).__name__}: {exc}"
            logger.error(f"{env.name}: fetch failed: {row['detail']}")
            continue
        # No digest: another builder finished it between the check and the lock.
        row["state"] = "built" if got.digest else "present"
        row["digest"] = got.digest
        row["bytes"] = got.size
        print(f"{row['state'].upper():<12} {env.name}  {got.size} bytes  {got.digest}",
              flush=True)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    logger.info(f"build-environments done: {summary}")

    built = [r for r in rows if r["state"] == "built"]
    if built:
        # Registration is not done here. Log the command for the operator.
        args = "".join(f" \\\n    --manifest {manifests / (r['environment'] + '.json')}"
                       for r in built)
        logger.info(
            "gate 11 - register what was materialised (operator):\n"
            f"  python -m clinicalomx.environments register --storage-root {root}{args}"
        )

    try:
        from prefect.artifacts import create_table_artifact

        create_table_artifact(
            key="environment-builds",
            table=rows,
            description=f"HPC SIF environments under {root}: {summary}",
        )
    except Exception as exc:
        print(f"table artifact publish failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)

    return {
        "storage_root": str(root),
        "manifest_dir": str(manifests),
        "counts": counts,
        "environments": rows,
        "dry_run": dry_run,
    }
