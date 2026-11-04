"""Refuse a copy of the `clinicalomx[extract]` dependency list anywhere
apart from the owner `[project.optional-dependencies].extract` in
`packages/clinicalomx/pyproject.toml`. Prevents drift of dependencies.

Usage:
    python tools/drift/extract_extra.py # exits 1 and names every copy
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "packages/clinicalomx/pyproject.toml"
THRESHOLD = 4


def extract_extra() -> list[str]:
    """The distribution names in the owner's `extract` extra, lowercased, version specifiers cut."""
    project = tomllib.loads((ROOT / OWNER).read_text())["project"]
    return [re.match(r"[A-Za-z0-9._-]+", r).group(0).lower()
            for r in project["optional-dependencies"]["extract"]]


def copies(names: list[str]) -> list[str]:
    """Every two-line window of a tracked file naming THRESHOLD or more of `names`.

    The owner pyproject and every `uv.lock` are skipped, and a match consumes both its lines so
    one wrapped list is reported once.
    """
    word = re.compile("|".join(
        rf"(?<![\w-]){'[-_]'.join(map(re.escape, re.split(r'[-_]', n)))}(?![\w-])" for n in names),
        re.I)
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                           check=True).stdout.decode().split("\0")
    found = []
    for rel in files:
        if not rel or rel == OWNER or rel.endswith("uv.lock"):
            continue
        try:
            text = (ROOT / rel).read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        lines = text.splitlines()
        skip = 0
        for n in range(len(lines)):
            if n < skip:
                continue
            hit = {m.group(0).lower().replace("_", "-")
                   for m in word.finditer("\n".join(lines[n:n + 2]))}
            if len(hit) >= THRESHOLD:
                first = n if word.search(lines[n]) else n + 1
                found.append(f"{rel}:{first + 1}: names {', '.join(sorted(hit))}")
                skip = n + 2
    return found


def main() -> int:
    """Print every copy; exit 1 if there was one."""
    found = copies(extract_extra())
    for f in found:
        print(f)
    if found:
        print(f"\n{len(found)} copy(ies) of the extract extra. Point at {OWNER} instead, "
              "or read it from there.", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
