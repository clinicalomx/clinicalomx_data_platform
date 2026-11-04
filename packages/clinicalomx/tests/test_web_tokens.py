"""Tests for the appearance seam: no module under `web/` names a design value.

`web/design/` is vendored verbatim from `docs/design/build-app/`. Outside it, a hex colour, a px or
rem length, a percentage width, a literal Radix colour scheme and a bare scale step are forbidden;
`width="100%"` is allowed. The seven states must be distinguishable without colour.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytest.importorskip("reflex", reason="the Extract Explorer needs clinicalomx[app]")

from clinicalomx.extract.app.web import tokens  # noqa: E402
from clinicalomx.extract.app.web.design import states  # noqa: E402
from clinicalomx.extract.app.web.design import tokens as contract  # noqa: E402

WEB = pathlib.Path(tokens.__file__).parent

#: Each forbidden pattern with the message to show. `design/` is not scanned.
FORBIDDEN = {
    r"#[0-9a-fA-F]{6}\b": "a hex colour",
    r"\b\d+px\b": "a pixel length",
    r'"\d+(?:\.\d+)?rem"': "a rem length",
    # A length only: `dot.py` writes the attribute name `width="` while stripping it from SVG.
    r'(?<!max_)width="(?!100%")[\w.%]+"': "a width that is not 100%",
    r'color_scheme="': "a literal colour scheme",
    # Radix layout props take a scale step; use `tokens.GAP`.
    r'\bspacing="\d"': "a bare spacing step rather than tokens.GAP",
    r'\bsize="\d"': "a bare size step",
}


def _modules():
    return sorted(p for p in WEB.glob("*.py") if p.name != "tokens.py")


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_but_tokens_decides_how_something_looks(module):
    found = []
    for line_no, line in enumerate(module.read_text().splitlines(), 1):
        if line.lstrip().startswith("#") or '"""' in line:
            continue  # prose, not a token
        for pattern, what in FORBIDDEN.items():
            if re.search(pattern, line):
                found.append(f"{module.name}:{line_no} carries {what}: {line.strip()}")
    assert not found, (
        "appearance belongs in web/design/, which is what a contract revision replaces:\n  "
        + "\n  ".join(found))


def test_the_scan_would_catch_something():
    assert re.search(r'color_scheme="', 'x = rx.callout(t, color_scheme="red")')
    assert re.search(r'\bspacing="\d"', "rx.vstack(a, spacing=\"3\")")


# The contract is carried, not adapted.
def test_the_theme_is_the_contracts_and_is_passed_through_whole():
    assert tokens.THEME is contract.THEME
    assert set(contract.THEME) >= {"appearance", "accent_color", "gray_color", "radius",
                                   "scaling", "panel_background"}
    assert contract.THEME["radius"] == "none", "the captain's one explicit correction"


def test_the_stylesheet_ships_inside_the_wheel_and_comes_out_of_it():
    assert tokens.stylesheets() == [contract.FONT_STYLESHEET]
    assert contract.FONT_STYLESHEET.startswith("https://fonts.googleapis.com")

    css = tokens.stylesheet()
    assert ".cx-table" in css and "--cx-row-h" in css, "tokens.css did not come out of the wheel"
    assert (WEB / "design" / "tokens.css").exists()


def test_the_vendored_contract_is_byte_identical_to_the_published_one():
    published = pathlib.Path(__file__).resolve().parents[3] / "docs" / "design" / "build-app"
    if not published.is_dir():
        pytest.skip("not a source checkout, so the published contract is not beside the wheel")
    for name in ("tokens.css", "tokens.py", "states.py"):
        assert (WEB / "design" / name).read_bytes() == (published / name).read_bytes(), \
            f"{name} in the package has drifted from docs/design/build-app/"


#: Every contract class that is a control: something a user clicks, types into or drags.
CONTROLS = (".cx-btn", ".cx-input", ".cx-select", ".cx-segmented__item",
            ".cx-add", ".cx-disclosure__head", ".cx-check")

#: `.cx-tab` is a control with no declared size floor in the contract. Excluded until the contract
#: declares one.
UNDECLARED_FLOOR = (".cx-tab",)


def _rule(css: str, selector: str) -> str:
    """The declaration block holding the selector, grouped selectors included. Strips comments."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for head, block in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if selector in [part.strip() for part in head.split(",")]:
            return block
    raise AssertionError(f"{selector} has no rule in the contract at all")


def test_the_undeclared_floors_are_named_rather_than_quietly_excluded():
    css = tokens.stylesheet()
    for selector in UNDECLARED_FLOOR:
        block = _rule(css, selector)
        assert "min-height" not in block, (
            f"{selector} declares a floor now, so move it into CONTROLS and delete this entry")


@pytest.mark.parametrize("selector", CONTROLS)
def test_a_control_is_never_smaller_than_the_target_floor_the_contract_states(selector):
    block = _rule(tokens.stylesheet(), selector)
    floors = [int(px) for px in re.findall(r"min-height:\s*(\d+)px", block)]
    assert floors, f"{selector} declares no height floor, so the browser default decides it"
    assert min(floors) >= 24, f"{selector} declares a target floor under 24px: {floors}"


def test_the_stylesheet_is_in_the_head_and_not_the_body():
    import reflex_base.registry as registry

    from clinicalomx.extract.app.web import make_app

    ctx = registry.RegistrationContext.ensure_context()
    token = registry.RegistrationContext._context_var.set(ctx.fork())
    try:
        app = make_app(live=False, title="Extract Explorer")
        head = "".join(str(c.render()) for c in app.head_components)
        body = str(next(iter(app._unevaluated_pages.values())).component().render())
    finally:
        registry.RegistrationContext._context_var.reset(token)
    # A selector, not a token name: the app writes `--cx-row-h` inline.
    marker = "::-webkit-details-marker"
    assert marker in head, "the design tokens are not in the document head"
    assert marker not in body, "the stylesheet is in the body, which flashes unstyled"


def test_the_disclosure_marker_is_suppressed_in_both_engines():
    css = tokens.stylesheet()
    assert "::-webkit-details-marker" in css
    assert "details[open]" in css, "the caret does not rotate when a disclosure opens"


def test_the_radix_gap_steps_come_from_the_contract_and_hold_only_at_100_percent():
    assert contract.THEME["scaling"] == "100%"
    assert set(tokens.GAP.values()) <= set(contract.SPACING_STEP.values()), \
        "a gap was named outside the contract's own step mapping"
    assert tokens.GAP["block"] == contract.SPACING_STEP["SPACE_3"]


# The state vocabulary.
def test_every_state_the_layer_reports_has_a_spec():
    import typing

    from clinicalomx.extract.availability import RoleState

    declared = set(typing.get_args(RoleState))
    assert declared <= set(states.STATES), declared - set(states.STATES)


def test_a_state_is_distinguishable_without_colour():
    glyphs = [spec.glyph for spec in states.STATES.values()]
    assert len(set(glyphs)) == len(glyphs), "two states share a glyph"
    for name in states.STATES:
        rendered = tokens.chip(name)
        assert f">{name}</span>" in rendered, f"{name} is drawn without its word"
        assert "<svg" in rendered, f"{name} is drawn without its glyph"


def test_three_states_carry_no_tint_at_all():
    assert set(states.TINTED) == {"denied", "dangling", "missing", "unplaceable"}
    for quiet in ("present", "unlinked", "undeclared"):
        assert not states.STATES[quiet].tinted


def test_a_verdict_is_drawn_from_a_different_shape_family_than_a_state():
    assert set(states.VERDICTS) == {"ready", "degraded", "blocked"}
    for name in states.VERDICTS:
        rendered = tokens.verdict(name)
        assert f">{name}</span>" in rendered
        assert "cx-verdict" in rendered and "cx-chip" not in rendered


def test_a_kind_is_drawn_with_the_mark_the_diagram_gives_it():
    from clinicalomx.extract.app.diagram import KIND_SHAPE

    for kind, shape in KIND_SHAPE.items():
        assert kind in states.KINDS, f"{kind} has a DOT shape and no table glyph"
        assert states.KINDS[kind].dot_shape == shape, (
            f"{kind} is {shape} in the picture and {states.KINDS[kind].dot_shape} in the table")


def test_an_unclassifiable_kind_gets_the_dashed_box_and_never_a_guess():
    assert "stroke-dasharray" in tokens.kind("")
    assert "stroke-dasharray" in tokens.kind("something-new")


def test_an_unknown_state_is_shown_rather_than_swallowed():
    assert "brand_new" in tokens.chip("brand_new")
    assert tokens.state_glyph_only("brand_new") == ""
    assert tokens.chip(None) == ""


def test_the_not_probed_dash_is_never_a_chip_and_never_a_zero():
    rendered = tokens.not_probed()
    assert "cx-notprobed" in rendered and "cx-chip" not in rendered
    assert f">{tokens.MISSING_NOT_PROBED}<" in rendered
    assert "title=" in rendered, "the reason has to be reachable, not only the dash"


def test_a_coverage_bar_never_replaces_its_number():
    rendered = tokens.coverage("4/12")
    assert ">4/12<" in rendered
    assert "cx-coverage__track" in rendered
    assert "cx-coverage--empty" in tokens.coverage("0/12")
    assert ">-<" in tokens.coverage("-"), "a fraction that is not one still shows its value"


def test_a_select_option_keeps_the_word_where_it_cannot_keep_the_glyph():
    assert tokens.state_text("present") == "present"
    assert "<svg" not in tokens.state_text("present")


def test_the_legend_runs_the_ladder_worst_first():
    items = tokens.legend_states()
    assert len(items) == len(states.LADDER)
    assert states.LADDER[0] == "undeclared" and states.LADDER[-1] == "present"
    assert [i[1] for i in items] == [states.STATES[n].legend for n in states.LADDER]


def test_the_legend_keeps_only_the_edge_half_of_the_diagrams_own_key():
    from clinicalomx.extract.app import diagram

    edges = tokens.legend_edges(diagram.legend())
    assert "derived from" in edges
    assert "\U0001f7e9" not in edges, "an emoji state survived the split"


def test_a_legend_whose_separator_moved_falls_back_to_the_whole_string():
    assert tokens.legend_edges("no separator here") == "no separator here"
