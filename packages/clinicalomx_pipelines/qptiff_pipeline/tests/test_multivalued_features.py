"""Every value-consuming read of an ROI role feature goes through ``newest_artifact`` or
``all_artifacts``. Role features are multi-valued. A static scan of this repo's source."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline"

# Features cx.annotate and cx.split write, so multi-valued. Names as they appear in source.
ROLE_FEATURES = (
    "F_THUMBNAIL",
    "F_PRED",
    "F_DET_OVERLAY",
    "F_ROI_ACCEPTED",
    "F_CORES_ACCEPTED",
    "F_DEARRAY_OVERLAY",
    "BGNORMED",
    "BGNORM_QC_FEATURE",
)

RESOLVERS = ("newest_artifact", "all_artifacts", "_art", "_resolve", "_mark", "_resolve_artifact")

# A presence check is safe: a non-empty set is truthy and not None.
PRESENCE = re.compile(r"is\s+(not\s+)?None|is\s+True|is\s+False|\bif\s+not\b")


def _feature_reads() -> list[tuple[Path, int, str]]:
    """Every source line that reads a role feature out of a feature-values dict.

    Line-based: keep such reads on one line, or assign to a named local first.
    """
    reads = []
    pattern = re.compile(rf"\.get\(\s*(?:vocab\.|states\.)?({'|'.join(ROLE_FEATURES)})\s*\)")
    for path in sorted(SRC.rglob("*.py")):
        if "/dev/" in str(path):
            continue  # the fixtures fabricate values
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                reads.append((path, n, line.strip()))
    return reads


def test_role_features_are_read_through_a_resolver():
    offenders = [
        (p.relative_to(SRC), n, line)
        for p, n, line in _feature_reads()
        if not any(r in line for r in RESOLVERS) and not PRESENCE.search(line)
    ]
    assert not offenders, "role feature read without newest_artifact/all_artifacts:\n" + "\n".join(
        f"  {p}:{n}  {line}" for p, n, line in offenders
    )


def test_the_reads_are_actually_being_found():
    reads = _feature_reads()
    assert len(reads) >= 8, f"only found {len(reads)} role-feature reads; regex likely stale"
