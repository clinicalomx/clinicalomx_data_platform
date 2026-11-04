#!/usr/bin/env python3
"""Render Grafana's alert rules from rules.template.yaml and this site's values.

Run: make ops-alert-rules. Reads CLINICALOMX_SITE_HPC_NAT_ADDRESS, which must parse as one IP
address, and CLINICALOMX_SITE_HPC_COMPUTE_SUBNET, a private IPv4 network. Both are written into a
LogQL regex with dots escaped. A refusal writes nothing.
Standard library only.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "rules.template.yaml"
RENDERED = HERE.parent / "provisioning" / "alerting" / "rules.yaml"
NAME = "CLINICALOMX_SITE_HPC_NAT_ADDRESS"
TOKEN = f"@{NAME}@"
SUBNET_NAME = "CLINICALOMX_SITE_HPC_COMPUTE_SUBNET"
SUBNET_TOKEN = f"@{SUBNET_NAME}@"


def subnet_pattern(cidr: str) -> str:
    """`cidr` as a regex alternation, one branch per whole-octet prefix it covers."""
    try:
        network = ipaddress.ip_network(cidr)
    except ValueError:
        network = None
    if network is None or network.version != 4 or not network.is_private:
        raise ValueError(f"{SUBNET_NAME} is unset or not a private IPv4 network; set "
                         "hpc.compute_subnet in site.yaml and render site.env with "
                         "scripts/site/render.py")
    octets = -(-network.prefixlen // 8)
    branches = []
    for part in network.subnets(new_prefix=octets * 8):
        prefix = re.escape(".".join(str(part.network_address).split(".")[:octets]))
        branches.append(prefix if octets == 4 else prefix + r"\..*")
    return "|".join(branches)


def render(template: str, address: str, subnet: str) -> str:
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(f"{NAME} is unset or not one IP address; set hpc.nat_address in site.yaml "
                         "and render site.env with scripts/site/render.py") from None
    pattern = subnet_pattern(subnet)
    for token in (TOKEN, SUBNET_TOKEN):
        if token not in template:
            raise ValueError(f"{TEMPLATE.name} no longer names {token}")
    return template.replace(TOKEN, re.escape(address)).replace(SUBNET_TOKEN, pattern)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    if os.geteuid() == 0:
        # Running as root: give the directory to the release tree's owner.
        owner = path.parent.parent.stat()
        os.chown(path.parent, owner.st_uid, owner.st_gid)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    # Grafana reads it as uid 472.
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main() -> int:
    try:
        text = render(TEMPLATE.read_text(), os.environ.get(NAME, ""),
                      os.environ.get(SUBNET_NAME, ""))
    except ValueError as exc:
        print(f"[alert-rules] REFUSED: {exc}", file=sys.stderr)
        return 1
    _write(RENDERED, text)
    print(f"[alert-rules] wrote {RENDERED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
