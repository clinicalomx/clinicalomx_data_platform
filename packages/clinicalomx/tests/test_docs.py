"""The generated vocabulary reference is deterministic, complete, byte-stable and database-free."""
from __future__ import annotations

import subprocess
import sys

from clinicalomx.names import ALL_FEATURES, ALL_TYPES
from clinicalomx.vocab.docs import render


def test_render_is_byte_stable():
    assert render() == render()


def test_every_foundation_name_appears():
    text = render()
    missing = [n for n in (*ALL_FEATURES, *ALL_TYPES) if n not in text]
    assert not missing, f"absent from the generated reference: {missing}"


def test_constants_are_mapped_back():
    text = render()
    for expected in ("FEATURES.ROI_UID", "FEATURES.PIPELINE_STATE", "TYPES.WHOLE_SLIDE"):
        assert expected in text, f"{expected} not mapped in the reference"


def test_deprecated_rows_are_shown_as_such():
    text = render()
    assert "slide_feature" in text
    assert "deprecated" in text


def test_render_needs_no_database():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from clinicalomx.vocab.docs import render; render();"
         " sys.exit(1 if 'lamindb' in sys.modules else 0)"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"render() pulled in lamindb\n{proc.stderr}"


def test_committed_reference_is_current():
    import pathlib

    ref = pathlib.Path(__file__).resolve().parent.parent / "docs" / "reference.md"
    if not ref.exists():
        import pytest

        pytest.skip("docs/reference.md not generated yet")
    assert ref.read_text() == render(), (
        "docs/reference.md is stale - regenerate with:\n"
        "  python -m clinicalomx.vocab docs > docs/reference.md"
    )
