"""Detect and refuse sensitive content during repository publications.

Usage:
    python tools/drift/publication.py                  # exits 1 and names every hit
    python tools/drift/publication.py --report-only    # the same report, then exits 0
    CLINICALOMX_DENYLIST=<vault file> python tools/drift/publication.py --snapshot
                                                       # the flip gate: denylist required
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
HEX32 = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32}(?![0-9A-Za-z])")
URL_PASSWORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@")
REFERENCE = re.compile(r"[$<{\[].*|%\(.*|[A-Z][A-Z0-9_]*")
IPV4 = re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9]|\.[0-9])")
NOT_PUBLIC = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
LOCK_VERSION = re.compile(r'(?:version = "|[A-Za-z0-9]-)$')  # a four-part version, as nvidia's are
HOST = re.compile(
    r"(?<![\w-])(?:[\w-]+\.)*(?:rc\.nectar\.org\.au|uq\.edu\.au)|qris[c]loud", re.IGNORECASE)
CLUSTER_PATH = re.compile(r"/scratch/use[r]/|/QRIS[d]ata/", re.IGNORECASE)
SCAN_NAME = re.compile(r"(?<![0-9])20\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])_[A-Za-z0-9-]+_")
SCT_WORD = re.compile(r"s[n]omed", re.IGNORECASE)
SCT_WORD_HOME = re.compile(r"packages/clinicalomx_omop/(?:src/clinicalomx_omop/athena\.py"
                           r"|README\.md|tests/.*|tests_pg/.*|spec/[^/]*\.csv)")
SCT_ID = re.compile(r"(?<![0-9A-Za-z])(?<![0-9]\.)[0-9]{6,18}(?![0-9A-Za-z]|\.[0-9])")
SCT_PARTITIONS = {"00", "01", "02", "10", "11", "12"}
# The vocabulary bundle's tables, as tests/test_athena.py names them, and the RF2 release prefix.
SCT_FILE = re.compile(r"(?:concept|concept_relationship|concept_ancestor|concept_synonym|vocabulary"
                      r"|source_to_concept_map|drug_strength|concept_class|relationship|domain)"
                      r"\.(?:csv|tsv|txt)|x?(?:sct|der)[12]_.*", re.IGNORECASE)
SITE_CONFIG = re.compile(
    r"\.env|\.env\..*|.*\.env|\.envrc|.*\.tfvars(?:\.json)?|users\.conf|site\.yaml")
EXAMPLE_REAL = {"environment.example": ".env"}  # every hub stack names its schema this way
PATIENT_DATA = "patient_etl_mono"
# Verhoeff's dihedral group D5: the multiplication and the position permutation tables.
_D = ["0123456789", "1234067895", "2340178956", "3401289567", "4012395678",
      "5987604321", "6598710432", "7659821043", "8765932104", "9876543210"]
_P = ["0123456789", "1576283094", "5803796142", "8916043527",
      "9453126870", "4286573901", "2793806415", "7046913258"]

ALLOWED = {
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P00338.json", 168, "sct/id"):
        "a Gene Ontology term number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P01732.json", 426, "sct/id"):
        "a Gene Ontology term number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P05023.json", 364, "sct/id"):
        "a Gene Ontology term number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P12830.json", 779, "sct/id"):
        "a Gene Ontology term number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/Q9BZS1.json", 1224, "sct/id"):
        "a Gene Ontology term number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/test_structure.py", 24, "sct/id"):
        "a Gene Ontology term number in the expected parse of a UniProt fixture, read by eye",
    (("packages/clinicalomx_pipelines/qptiff_pipeline/src/qptiff_pipeline/review/roi_canvas/"
      "frontend/fabric.min.js"), 1, "sct/id"):
        "a digit run inside a CSS colour literal in the vendored, minified fabric.js, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/O75390.json", 24, "host/ipv4"):
        "an Enzyme Commission number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P00338.json", 34, "host/ipv4"):
        "an Enzyme Commission number in a recorded UniProt response, read by eye",
    ("packages/clinicalomx_markers/tests/fixtures/uniprot/P05023.json", 29, "host/ipv4"):
        "an Enzyme Commission number in a recorded UniProt response, read by eye",
    ("packages/clinicalomxdb/tests/explorer/test_api.py", 116, "credential/url-password"):
        "a synthetic credential that test asserts never reaches the page, read by eye",
    ("scripts/site/test_render.py", 83, "credential/url-password"):
        "a synthetic credential under example.edu that render.py must refuse, read by eye",
    ("infra/control_plane/network.tf", 97, "host/ipv4"):
        "Google's and Cloudflare's public DNS resolvers, the same for any site, read by eye",
    ("infra/control_plane/variables.tf", 17, "host/ipv4"):
        "LaminHub's egress addresses, a property of that service rather than of this site, read by eye",
    ("NOTICE", 88, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_ml/pyproject.toml", 136, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_ml/uv.lock", 588, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_ml/uv.lock", 931, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_pipelines/qptiff_pipeline/containers/Dockerfile.extract", 65, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_pipelines/qptiff_pipeline/containers/Dockerfile.qptiff", 63, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_pipelines/uv.lock", 569, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx_pipelines/uv.lock", 908, "denylist"):
        "where the builds fetch the bioio-qptiff dependency from, which must stay, read by eye",
    ("packages/clinicalomx/curation_app/reflex.lock/bun.lock", 201, "denylist"):
        "three letters inside a sha512 integrity hash, read by eye",
    ("packages/clinicalomx/reflex.lock/bun.lock", 201, "denylist"):
        "three letters inside a sha512 integrity hash, read by eye",
    ("packages/clinicalomx_pipelines/reflex.lock/bun.lock", 201, "denylist"):
        "three letters inside a sha512 integrity hash, read by eye",
}


def verhoeff(digits: str) -> bool:
    """True if `digits` carries a valid Verhoeff check digit, as every SCT identifier does."""
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = int(_D[c][int(_P[i % 8][int(d)])])
    return c == 0


def public(text: str) -> bool:
    """True if `text` is an IP address that would route on the internet."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:  # an octet over 255: a version string, not an address
        return False
    return not any(ip in net for net in NOT_PUBLIC)


def line_classes(rel: str, line: str) -> set[str]:
    """Every class this one line triggers. A class fires at most once per line."""
    found = set()
    if GITHUB_TOKEN.search(line):
        found.add("credential/github-token")
    if JWT.search(line):
        found.add("credential/jwt")
    if any(not REFERENCE.fullmatch(m.group(1)) for m in URL_PASSWORD.finditer(line)):
        found.add("credential/url-password")
    lock = rel.endswith("uv.lock")
    if any(public(m.group(0)) and not (lock and LOCK_VERSION.search(line[:m.start()]))
           for m in IPV4.finditer(line)):
        found.add("host/ipv4")
    if HOST.search(line):
        found.add("host/site-domain")
    if CLUSTER_PATH.search(line):
        found.add("cluster/path")
    if SCAN_NAME.search(line):
        found.add("patient/scan")
    if not SCT_WORD_HOME.fullmatch(rel) and SCT_WORD.search(line):
        found.add("sct/word")
    for m in SCT_ID.finditer(line):
        s = m.group(0)
        if lock and line[:m.start()].endswith("size = "):
            continue  # uv records each distribution's byte count there
        if s[-3:-1] in SCT_PARTITIONS and verhoeff(s):
            found.add("sct/id")
    return found


def withheld(rel: str, deny: re.Pattern | None) -> str:
    """`rel` with every value-shaped span replaced, so a path can be printed safely."""
    for rx in (GITHUB_TOKEN, JWT, HEX32, URL_PASSWORD, IPV4, HOST, CLUSTER_PATH, SCAN_NAME,
               SCT_WORD, SCT_ID, *([deny] if deny else [])):
        rel = rx.sub("<withheld>", rel)
    return rel


def ec2_pairs(lines: list[str]) -> set[int]:
    """Line numbers holding a 32-hex token within three lines of a DIFFERENT one.

    One such token is a hash; two distinct ones close together are an access and secret pair.
    """
    tokens = [(n, m.group(0).lower())
              for n, line in enumerate(lines, 1) for m in HEX32.finditer(line)]
    return {n for n, t in tokens for k, u in tokens if t != u and abs(n - k) <= 3}


def denylist() -> re.Pattern | None:
    """The vault denylist as one pattern, or None when CLINICALOMX_DENYLIST is unset.

    Exits if the file is inside this checkout, unreadable or empty. Committing the list would
    publish exactly what it lists, so an in-checkout path is a refusal and not a warning.
    """
    path = os.environ.get("CLINICALOMX_DENYLIST")
    if not path:
        return None
    file = Path(path).resolve()
    if file.is_relative_to(ROOT):
        sys.exit("CLINICALOMX_DENYLIST names a file inside this checkout; keep it in the vault.")
    try:
        values = [v.strip() for v in file.read_text().splitlines()]
    except OSError as e:
        sys.exit(f"CLINICALOMX_DENYLIST cannot be read: {e.strerror}")
    values = [v for v in values if v and not v.startswith("#")]
    if not values:
        sys.exit("CLINICALOMX_DENYLIST names a file with no entries.")
    return re.compile("|".join(map(re.escape, values)), re.IGNORECASE)


def site_config(files: list[str]) -> list[tuple[str, int, str]]:
    """Site config files that are tracked, and `.example` files whose real sibling is not ignored.

    An example is only safe if git would refuse its real counterpart; otherwise the next person
    to fill it in commits their site.
    """
    found, real = [], {}
    for rel in files:
        name = Path(rel).name
        if name.endswith(".example"):
            sibling = EXAMPLE_REAL.get(name, name.removesuffix(".example"))
            if SITE_CONFIG.fullmatch(sibling):
                real[str(Path(rel).with_name(sibling))] = rel
        elif SITE_CONFIG.fullmatch(name):
            found.append((rel, 0, "site-config"))
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "--stdin", "-z"], cwd=ROOT,
                             input="\0".join(real).encode(), capture_output=True,
                             check=False).stdout.decode()  # exits 1 when it ignores none
    found += [(example, 0, "site-config") for path, example in real.items()
              if path not in ignored.split("\0")]
    return found


def engine_allowlists(tracked: set[str]) -> list[tuple[str, int, str]]:
    """Entries in the gitleaks and detect-secrets allowlists with no reason, or naming a dead file.

    A stale entry is a finding because it silently widens what the engines skip as the tree moves.
    """
    found = []
    reason = False
    for n, line in enumerate((ROOT / ".gitleaksignore").read_text().splitlines(), 1):
        line = line.strip()
        reason = line.startswith("#") or (reason and bool(line))
        if line and not line.startswith("#"):
            if not reason:
                found.append((".gitleaksignore", n, "allowlist/no-reason"))
            if line.split(":")[0] not in tracked:
                found.append((".gitleaksignore", n, "allowlist/stale"))
    baseline = json.loads((ROOT / ".secrets.baseline").read_text())
    for rel, results in baseline["results"].items():
        for r in results:
            if not str(r.get("reason", "")).strip():
                found.append((rel, r["line_number"], "allowlist/no-reason in .secrets.baseline"))
        if rel not in tracked:
            found.append((rel, 0, "allowlist/stale in .secrets.baseline"))
    return found


def git(*args: str) -> str:
    """`git` in the repository root, stdout decoded. Raises on a non-zero exit."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout.decode()


def history() -> list[tuple[str, int, str]]:
    """Findings if this checkout is not a single root commit on main alone.

    The public repository starts from one commit; an inherited history would carry everything
    the scrub removed.
    """
    found = []
    if git("rev-list", "--all", "--count").strip() != "1":
        found.append((".git", 0, "history/commits"))
    refs = git("for-each-ref", "--format=%(refname)").split()
    kept = re.compile(r"refs/heads/main|refs/remotes/[^/]+/(?:main|HEAD)|refs/tags/.+")
    if any(not kept.fullmatch(r) for r in refs):
        found.append((".git", 0, "history/refs"))
    return found


def hits(deny: re.Pattern | None) -> list[tuple[str, int, str]]:
    """Every finding in the tracked tree, as `(path, line, class)`. Line 0 means the whole file."""
    files = [f for f in git("ls-files", "-z").split("\0") if f]
    found = site_config(files) + engine_allowlists(set(files))
    for rel in files:
        if PATIENT_DATA in rel:
            found.append((rel, 0, "patient/data"))
            continue
        if SCT_FILE.fullmatch(Path(rel).name):
            found.append((rel, 0, "sct/file"))
        found += [(rel, 0, c) for c in sorted(line_classes(rel, rel))]
        if deny and deny.search(rel):
            found.append((rel, 0, "denylist"))
        if rel.endswith(".ipynb"):
            found.append((rel, 0, "notebook"))
            continue
        try:
            lines = (ROOT / rel).read_text().splitlines()
        except UnicodeDecodeError:
            found.append((rel, 0, "binary"))
            continue
        except (IsADirectoryError, FileNotFoundError):
            continue
        found += [(rel, n, "credential/ec2-pair") for n in sorted(ec2_pairs(lines))]
        for n, line in enumerate(lines, 1):
            found += [(rel, n, c) for c in sorted(line_classes(rel, line))]
            if deny and deny.search(line):
                found.append((rel, n, "denylist"))
    return found


def main() -> int:
    """Print the findings grouped by class, then each one. Exit 1 unless report-only."""
    snapshot = "--snapshot" in sys.argv[1:]
    report_only = "--report-only" in sys.argv[1:]
    if snapshot and report_only:
        sys.exit("--snapshot is the flip gate and is never report-only.")
    deny = denylist()
    if snapshot and deny is None:
        sys.exit("--snapshot needs CLINICALOMX_DENYLIST: the flip gate is not run on shapes alone.")
    found = hits(deny) + (history() if snapshot else [])
    keys = set(found)
    found = [f for f in found if f not in ALLOWED]
    found += [(rel, n, "allowlist/no-reason")
              for (rel, n, c), why in ALLOWED.items() if not why.strip()]
    # A denylist entry can only be seen stale when the denylist was read.
    found += [(rel, n, "allowlist/stale") for rel, n, c in ALLOWED
              if (rel, n, c) not in keys and (deny or c != "denylist")]
    for rel, n, c in found:
        print(f"{withheld(rel, deny)}:{n}: {c}")
    if deny is None:
        print("CLINICALOMX_DENYLIST is unset: the exact site values were NOT checked, only shapes.",
              file=sys.stderr)
    if found:
        print(f"\n{len(found)} publication finding(s). Remove the value, move it behind the site "
              "configuration, or review it and add a reasoned ALLOWED entry.", file=sys.stderr)
        for c, n in Counter(c for _, _, c in found).most_common():
            print(f"{n:6d}  {c}", file=sys.stderr)
    return 1 if found and not report_only else 0


if __name__ == "__main__":
    sys.exit(main())
