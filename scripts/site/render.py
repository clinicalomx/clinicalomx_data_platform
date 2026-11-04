#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "pyyaml>=6"]
# ///
"""Validate site.yaml and render the two files every consumer reads.

site.yaml holds every fact that ties this platform to one deployment site; site.example.yaml at
the repository root documents each key. This is the ONLY reader of that file. Every consumer
reads environment variables instead, the one format each already understands:

    site.env      KEY=VALUE lines: systemd EnvironmentFile, compose and make interpolation,
                  `prefect deploy` ({{ $NAME }}), and the shell (`set -a; . site.env`)
    users.conf    the HPC users, one per line, read by the HPC worker bootstraps and the portal

Both are written into this tree, at the paths their readers already use, and both are
gitignored. On the hub each is a symlink into config/package-env/ (assemble-release.sh), so a
write here lands in the one copy every release reads.

    uv run scripts/site/render.py [site.yaml]           validate, then write both files
    uv run scripts/site/render.py --check [site.yaml]   validate only
    uv run scripts/site/render.py --check-github        compare the GitHub variables the release
                                                        workflows read with site.yaml

The model is strict: every key is required, an unknown key is refused, and a value still in
<...> form or shaped like a secret is refused, naming the key and never the value.
"""

import argparse
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

ROOT = Path(__file__).resolve().parents[2]
SITE_YAML = ROOT / "site.yaml"
SITE_ENV = ROOT / "site.env"
USERS_CONF = ROOT / "packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf"

PLACEHOLDER = re.compile(r"<[^<>]*>")

# Shapes from the publication scanner's credential class. A match is refused whatever key it is
# under: a secret belongs in a Prefect block, a hub .env file or the lab vault.
SECRET_SHAPES = {
    "a GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "a JSON web token": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "a credential inside a URL": re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    "a long hex string (an API key or EC2 pair)": re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"),
    "an AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a webhook URL": re.compile(r"hooks\.slack\.com|webhook\.office\.com|\.logic\.azure\.com|/api/webhooks/"),
}

# What a value may contain once it is a line of site.env. systemd, make and the shell all read
# that file, and each treats quotes, `$`, `#` and whitespace differently.
ENV_SAFE = re.compile(r"[A-Za-z0-9_./:@,+=-]*")


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk(item, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def _absolute(value: str) -> str:
    if not value.startswith("/") or value != value.rstrip("/") or "//" in value:
        raise ValueError("must be an absolute path with no trailing or doubled slash")
    return value


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Hub(_Section):
    public_address: str = Field(pattern=r"^[A-Za-z0-9.-]+$")
    compute_ingress_cidr: str
    portal_url: str = Field(pattern=r"^https?://[^/\s]+$")

    @field_validator("compute_ingress_cidr")
    @classmethod
    def _cidr(cls, value: str) -> str:
        ipaddress.ip_network(value)
        return value


class Lamin(_Section):
    instance: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")


class Storage(_Section):
    hpc_root: str
    hpc_alias: str
    raw_root: str
    raw_scratch_root: str
    model_root: str
    container_binds: tuple[str, ...] = Field(min_length=1)

    @field_validator("hpc_root", "hpc_alias", "raw_root", "raw_scratch_root", "model_root")
    @classmethod
    def _path(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("container_binds")
    @classmethod
    def _binds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_absolute(v) for v in value)

    @model_validator(mode="after")
    def _roots_are_bound(self) -> "Storage":
        for name in ("hpc_root", "hpc_alias", "raw_root", "raw_scratch_root", "model_root"):
            if not any(_under(getattr(self, name), bind) for bind in self.container_binds):
                raise ValueError(f"storage.{name} is under none of storage.container_binds, so no "
                                 "compute container could read it")
        return self


class Hpc(_Section):
    login_host: str = Field(pattern=r"^[A-Za-z0-9.-]+$")
    slurm_account: str = Field(pattern=r"^[\w.-]+$")
    default_partition: str = Field(pattern=r"^[\w.-]+$")
    default_qos: str = Field(pattern=r"^[\w.-]+$")
    user_scratch_root: str
    users: tuple[str, ...] = Field(min_length=1)
    nat_address: str
    compute_subnet: str

    @field_validator("user_scratch_root")
    @classmethod
    def _path(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("nat_address")
    @classmethod
    def _address(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    @field_validator("compute_subnet")
    @classmethod
    def _subnet(cls, value: str) -> str:
        network = ipaddress.ip_network(value)
        if network.version != 4 or not network.is_private:
            raise ValueError("must be a private IPv4 network")
        return value

    @field_validator("users")
    @classmethod
    def _users(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("a user is listed twice")
        for user in value:
            if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
                raise ValueError("each user must be a plain lowercase login name")
        return value


class Registry(_Section):
    images: str = Field(pattern=r"^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+$")


class Packages(_Section):
    index_url: Annotated[str, StringConstraints(pattern=r"^https?://[^/\s]+/\S*$")] | None


class Source(_Section):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")


class Site(_Section):
    schema_version: Literal[1]
    hub: Hub
    lamin: Lamin
    storage: Storage
    hpc: Hpc
    registry: Registry
    packages: Packages
    source: Source

    @model_validator(mode="before")
    @classmethod
    def _no_placeholder_or_secret(cls, data: Any, info: ValidationInfo) -> Any:
        example = bool(info.context and info.context.get("example"))
        problems = []
        for path, value in _walk(data):
            if not example and PLACEHOLDER.search(value):
                problems.append(f"{path} is still a <...> placeholder")
            for shape, pattern in SECRET_SHAPES.items():
                if pattern.search(value):
                    problems.append(f"{path} is shaped like {shape}; put the NAME of the block or "
                                    "env key here, never the value")
        if problems:
            raise ValueError("; ".join(problems))
        return data

    @model_validator(mode="after")
    def _nat_is_admitted(self) -> "Site":
        # One NAT, two keys: the security group admits the CIDR, the alert allows the address.
        if ipaddress.ip_address(self.hpc.nat_address) not in ipaddress.ip_network(
                self.hub.compute_ingress_cidr):
            raise ValueError("hpc.nat_address is outside hub.compute_ingress_cidr, so the security "
                             "group would drop every compute job; change both when the NAT moves")
        return self


def load(path: Path, *, example: bool = False) -> Site:
    """Parse and validate. `example` unwraps each `<...>` and skips the placeholder refusal."""
    data = yaml.safe_load(path.read_text())
    if example:
        data = _unwrap(data)
    return Site.model_validate(data, context={"example": example})


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    return value


def _origin(url: str | None) -> str:
    if url is None:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def environment(site: Site) -> dict[str, str]:
    """Every name site.env carries, in the order it writes them."""
    address = site.hub.public_address
    return {
        # Derived: the two addresses a compute job reaches the hub on.
        "PREFECT_EXTERNAL_API_URL": f"http://{address}:4200/api",
        "MLFLOW_EXTERNAL_TRACKING_URI": f"http://{address}:5000",
        "OPS_PORTAL_URL": site.hub.portal_url,
        "LAMIN_INSTANCE": site.lamin.instance,
        "CLINICALOMX_SITE_HPC_ROOT": site.storage.hpc_root,
        "CLINICALOMX_SITE_HPC_ALIAS": site.storage.hpc_alias,
        "CLINICALOMX_SITE_RAW_ROOT": site.storage.raw_root,
        "CLINICALOMX_SITE_RAW_SCRATCH_ROOT": site.storage.raw_scratch_root,
        "CLINICALOMX_SITE_MODEL_ROOT": site.storage.model_root,
        "CLINICALOMX_SITE_CONTAINER_BINDS": ",".join(site.storage.container_binds),
        "CLINICALOMX_SITE_HPC_LOGIN_HOST": site.hpc.login_host,
        "CLINICALOMX_SITE_HPC_USER": site.hpc.users[0],
        "CLINICALOMX_SITE_SLURM_ACCOUNT": site.hpc.slurm_account,
        "CLINICALOMX_SITE_SLURM_PARTITION": site.hpc.default_partition,
        "CLINICALOMX_SITE_SLURM_QOS": site.hpc.default_qos,
        "CLINICALOMX_SITE_USER_SCRATCH_ROOT": site.hpc.user_scratch_root,
        "CLINICALOMX_SITE_HPC_NAT_ADDRESS": site.hpc.nat_address,
        "CLINICALOMX_SITE_HPC_COMPUTE_SUBNET": site.hpc.compute_subnet,
        "CLINICALOMX_SITE_IMAGE_REGISTRY": site.registry.images,
        # Derived: the registry's owner, which a SIF key carries (environments/<owner>/...).
        "CLINICALOMX_SITE_IMAGE_OWNER": site.registry.images.rsplit("/", 1)[-1],
        "CLINICALOMX_SITE_REPOSITORY": site.source.repository,
    }


HEADER = ("# GENERATED by scripts/site/render.py from site.yaml. Do not edit: change site.yaml and "
          "render again.\n")


def render_env(site: Site) -> str:
    lines = []
    for name, value in environment(site).items():
        if not ENV_SAFE.fullmatch(value):
            raise ValueError(f"{name} holds a character site.env cannot carry safely "
                             "(whitespace, a quote, $, # or a backslash)")
        lines.append(f"{name}={value}\n")
    return HEADER + "".join(lines)


def render_users(site: Site) -> str:
    return (HEADER + "# The HPC users the hub submits as, from hpc.users. The first is the primary.\n"
            + "".join(f"{user}\n" for user in site.hpc.users))


def _write(path: Path, text: str) -> None:
    # Resolve first: on the hub this path is a symlink into config/package-env/, and replacing
    # the link with a file would leave every other release reading the old copy.
    target = path.resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def github_variables(site: Site) -> dict[str, str]:
    """The repository variables the release workflows read; CI cannot read site.yaml to compare.

    An S3-backed index is read path-style, so its endpoint is the index URL's origin and its
    bucket is the first path segment.
    """
    url = site.packages.index_url
    return {
        "UV_S3_ENDPOINT_URL": _origin(url),
        "INDEX_BUCKET": urlsplit(url).path.split("/")[1] if url else "",
    }


def check_github(site: Site) -> list[str]:
    wanted = github_variables(site)
    problems = []
    for name, value in wanted.items():
        got = subprocess.run(["gh", "variable", "get", name, "--repo", site.source.repository],
                             check=False, capture_output=True, text=True)
        if got.returncode != 0:
            problems.append(f"repository variable {name} is not set on {site.source.repository}")
        elif got.stdout.strip() != value:
            problems.append(f"repository variable {name} differs from site.yaml")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site", nargs="?", type=Path, default=SITE_YAML)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate only; write nothing")
    mode.add_argument("--check-github", action="store_true",
                      help="compare the GitHub repository variables with site.yaml")
    args = parser.parse_args(argv)

    if not args.site.is_file():
        print(f"no site file at {args.site}: copy site.example.yaml to site.yaml and fill it in",
              file=sys.stderr)
        return 1
    try:
        site = load(args.site)
        env, users = render_env(site), render_users(site)
    except ValidationError as exc:
        # include_input=False: the input may be the secret this refusal exists to keep out of logs.
        for error in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(part) for part in error["loc"]) or "site"
            print(f"REFUSED {args.site}: {where}: {error['msg']}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"REFUSED {args.site}: {exc}", file=sys.stderr)
        return 1

    if args.check_github:
        problems = check_github(site)
        for problem in problems:
            print(f"DIFFERS: {problem}", file=sys.stderr)
        print("github variables match site.yaml" if not problems else "")
        return 1 if problems else 0
    if args.check:
        print(f"OK: {args.site} is a valid site file")
        return 0
    _write(SITE_ENV, env)
    _write(USERS_CONF, users)
    print(f"wrote {SITE_ENV.relative_to(ROOT)} and {USERS_CONF.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
