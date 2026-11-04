"""Refuse a specimen accession in any tracked file.

Slide names carry the pathology accession they were cut from, and this repository is going
public. Accessions reached git through notebook cell outputs, a test fixture and a docstring
example; the real ones live in lamindb, and anything here that needs a slide name uses a
synthetic one.

An ACCESSION is a token of one of three shapes, in any case, not preceded by a letter or digit:
`PA<digits>P<digits>`, `RB<digits>P<digits>` and `A5<digits>CR<digits>`, where a run of spaces,
underscores, hyphens or ampersands may split the letter and digit groups, and the group after
`P` or `CR` has at least three digits. The lookbehind and that three-digit floor are what keep
base64 (an embedded image in a notebook output) from matching mid-string: case-insensitive
matching without the floor flagged one base64 run in a tracked notebook. The report names the
file, the line and the SHAPE, never the token, so a CI log does not republish what it found.

This is a HEURISTIC and it misses: one split by any other separator (a dot, a slash), one with
fewer than three digits after `P` or `CR`, a shape not listed, a name in an untracked or binary
file, and a token glued to a preceding letter or digit.

    python tools/drift/specimen_names.py     # exits 1 and names every hit
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCESSION = re.compile(
    r"(?<![A-Za-z0-9])(?:PA\d+[ _&-]*P\d{3,}|RB\d+[ _&-]*P\d{3,}|A5\d+[ _&-]*CR\d{3,})", re.IGNORECASE)


def hits() -> list[str]:
    """Every accession-shaped token in a tracked file, as `path:line: shape`."""
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                           check=True).stdout.decode().split("\0")
    found = []
    for rel in files:
        if not rel:
            continue
        try:
            text = (ROOT / rel).read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in ACCESSION.finditer(line):
                found.append(f"{rel}:{n}: {re.sub(r'[0-9]+', '<digits>', m.group(0))}")
    return found


def main() -> int:
    """Print every hit; exit 1 if there was one."""
    found = hits()
    for f in found:
        print(f)
    if found:
        print(f"\n{len(found)} accession-shaped token(s). Use a synthetic name; for a notebook, "
              "clear its outputs.", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
