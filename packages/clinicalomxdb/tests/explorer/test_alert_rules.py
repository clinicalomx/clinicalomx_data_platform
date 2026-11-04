"""The rendered alert rules allow the facility NAT address exactly, and the compute subnet.

Renders hub-ops/grafana/alerting/render.py with a documentation-range address and a synthetic
private subnet, and runs the extracted LogQL regex as a fullmatch: dots are escaped, look-alikes
are refused, the rest of the allowlist is unchanged, and a bad value writes no file.
"""

import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout supplies what make explorer-test mounts.
GRAFANA = pathlib.Path("/ops-grafana")
if not GRAFANA.is_dir():
    GRAFANA = HERE.parent.parent / "hub-ops/grafana"

spec = importlib.util.spec_from_file_location("alert_rules_render", GRAFANA / "alerting/render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

TEMPLATE = (GRAFANA / "alerting/rules.template.yaml").read_text()
NAT = "198.51.100.7"
SUBNET = "10.10.0.0/23"

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def allowlist(rendered):
    """The regex after `request_remote_ip !~` in the unexpected-source rule."""
    for group in yaml.safe_load(rendered)["groups"]:
        for rule in group["rules"]:
            if rule["uid"] == "comx-unexpected-api-source":
                expr = rule["data"][0]["model"]["expr"]
                return re.compile(re.search(r"request_remote_ip !~ `([^`]*)`", expr)[1])
    raise AssertionError("comx-unexpected-api-source is not in the rendered rules")


def allowed(pattern, address):
    return pattern.fullmatch(address) is not None


rendered = render.render(TEMPLATE, NAT, SUBNET)
pattern = allowlist(rendered)
check("the NAT address is allowed", allowed(pattern, NAT), pattern.pattern)
check("its dots are escaped in the rule", r"198\.51\.100\.7|" in pattern.pattern, pattern.pattern)
check("any character in place of a dot is not allowed",
      not allowed(pattern, "198x51y100z7"), pattern.pattern)
check("a longer address sharing the prefix is not allowed", not allowed(pattern, NAT + "0"))
check("an unrelated public address is not allowed", not allowed(pattern, "203.0.113.10"))
for address in ("10.10.0.9", "10.10.1.200", "172.17.0.1", "172.31.255.1", "127.0.0.1"):
    check(f"{address} is still allowed", allowed(pattern, address))
for address in ("10.10.2.1", "10.11.0.1", "10.100.0.1"):
    check(f"{address}, outside the compute subnet, is not allowed", not allowed(pattern, address))

loose = allowlist(TEMPLATE.replace(render.TOKEN, NAT)
                  .replace(render.SUBNET_TOKEN, render.subnet_pattern(SUBNET)))
check("contrast: an unescaped render WOULD allow the dotless address",
      allowed(loose, "198x51y100z7"), loose.pattern)

check("the render differs from the template only at the tokens",
      rendered == TEMPLATE.replace(render.TOKEN, r"198\.51\.100\.7")
      .replace(render.SUBNET_TOKEN, r"10\.10\.0\..*|10\.10\.1\..*"))
check("no token survives the render", not re.search(r"@CLINICALOMX_SITE_[A-Z0-9_]+@", rendered))

v6 = allowlist(render.render(TEMPLATE, "2001:db8::7", SUBNET))
check("an IPv6 NAT is allowed exactly",
      allowed(v6, "2001:db8::7") and not allowed(v6, "2001:db8::70"))

for bad in ("", "198.51.100.0/24", ".*", NAT + "|.*", "nat.example.edu"):
    try:
        render.render(TEMPLATE, bad, SUBNET)
        check(f"{bad!r} is refused", False, "rendered")
    except ValueError as exc:
        check(f"{bad!r} is refused", render.NAME in str(exc), str(exc))

for bad in ("", "10.10.0.1/23", "0.0.0.0/0", "fd00::/64", ".*"):
    try:
        render.render(TEMPLATE, NAT, bad)
        check(f"subnet {bad!r} is refused", False, "rendered")
    except ValueError as exc:
        check(f"subnet {bad!r} is refused", render.SUBNET_NAME in str(exc), str(exc))

# A temp copy of the layout, so nothing lands in the checkout.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    shutil.copytree(GRAFANA / "alerting", root / "alerting",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "provisioning").mkdir()
    out = root / "provisioning/alerting/rules.yaml"
    env = {k: v for k, v in os.environ.items() if k not in (render.NAME, render.SUBNET_NAME)}

    refused = subprocess.run([sys.executable, str(root / "alerting/render.py")],
                             env=env, capture_output=True, text=True)
    check("an unset address fails the script", refused.returncode == 1, refused.stderr)
    check("and writes no file", not out.exists())

    done = subprocess.run([sys.executable, str(root / "alerting/render.py")],
                          env={**env, render.NAME: NAT, render.SUBNET_NAME: SUBNET},
                          capture_output=True, text=True)
    check("a set address renders", done.returncode == 0, done.stderr)
    check("to the file Grafana provisions", out.is_file() and out.read_text() == rendered)
    check("readable by Grafana's uid", out.is_file() and out.stat().st_mode & 0o777 == 0o644)

print("=" * 70)
print("FAILED:", ", ".join(failures) if failures else "nothing")
sys.exit(1 if failures else 0)
