"""The end-user docs must not name features, commands or recipes that do not exist."""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).parent.parent
DOCS = [ROOT / "docs" / "extract.md", ROOT / "README.md"]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_document_names_a_feature_constant_that_does_not_exist(path):
    from clinicalomx.names import FEATURES, TYPES

    text = path.read_text()
    for cls, label in ((FEATURES, "FEATURES"), (TYPES, "TYPES")):
        for attr in re.findall(rf"\b(?:cx\.)?{label}\.([A-Z_]+)\b", text):
            assert hasattr(cls, attr), (
                f"{path.name} names {label}.{attr}, which does not exist. If it moved to a "
                f"consumer repo, show it as that repo's own constant.")


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_document_names_a_cli_subcommand_that_does_not_exist(path):
    for cmd in re.findall(r"clinicalomx-extract (\w[\w-]*)", path.read_text()):
        assert cmd in _cli_commands(), (
            f"{path.name} documents `clinicalomx-extract {cmd}`, which is not a command")


def _cli_commands() -> set[str]:
    """The subcommands the CLI offers, parsed from its own help."""
    import contextlib
    import io

    import clinicalomx.extract.__main__ as cli

    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        cli.main(["--help"])
    listed = re.search(r"\{([\w,-]+)\}", buf.getvalue())
    assert listed, "could not read the subcommand list out of --help"
    return set(listed.group(1).split(","))


def test_the_documented_commands_are_all_real():
    walked = set(re.findall(r"clinicalomx-extract (\w[\w-]*)",
                            (ROOT / "docs" / "extract.md").read_text()))
    assert {"find", "describe", "plan", "assemble"} <= walked, "the guide skips a step"
    assert walked <= _cli_commands()


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_document_names_a_recipe_that_does_not_exist(path):
    from clinicalomx.extract.recipes import FOUNDATION_RECIPES

    known = {r.name for r in FOUNDATION_RECIPES}
    for name in re.findall(r"`?\b((?:spatialdata|ometiff)\.[\w.-]+)`?", path.read_text()):
        assert name.rstrip(".") in known, f"{path.name} names recipe {name!r}, which does not exist"
