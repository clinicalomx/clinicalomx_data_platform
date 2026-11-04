"""The adapter between the app and the vendored design contract in `web/design/`.

It serves `tokens.css` from inside the wheel, renders states, verdicts, kinds and coverage as
markup, and gives the plain-text spelling of a state. No other module under `web/` may name a
colour, a length or a scale step; `tests/test_web_tokens.py` checks this.
"""
from __future__ import annotations

import html

from clinicalomx.extract.app.web.design import states as _states
from clinicalomx.extract.app.web.design import tokens as _tokens

#: Re-exported from the contract.
THEME = _tokens.THEME
APP_STYLE = _tokens.APP_STYLE
DENSITY = _tokens.DENSITY

#: The app's named gaps, as Radix scale steps through the contract's `SPACING_STEP`. Valid only
#: while `THEME["scaling"]` is "100%".
GAP = {
    "hair": _tokens.SPACING_STEP["SPACE_1"],
    "row": _tokens.SPACING_STEP["SPACE_2"],
    "block": _tokens.SPACING_STEP["SPACE_3"],
    "section": _tokens.SPACING_STEP["SPACE_4"],
    "page": _tokens.SPACING_STEP["SPACE_5"],
    "wide": _tokens.SPACING_STEP["SPACE_6"],
}

#: Maximum height of a long code block: the contract's table bound.
CODE_MAX_H = _tokens.TABLE_MAX_H

#: What every MISSING column prints: nothing here probes bytes.
MISSING_NOT_PROBED = "-"
NOT_PROBED_TITLE = "not probed - this app reads the catalogue only"


def stylesheets() -> list[str]:
    """The stylesheets Reflex fetches by URL: the type faces only.

    `tokens.css` is inlined through :func:`stylesheet` instead.
    """
    return [_tokens.FONT_STYLESHEET]


def stylesheet() -> str:
    """`tokens.css` from the installed package, for a `<style>` element in the document head."""
    return _tokens.tokens_css()


# The state vocabulary, as markup.
def chip(state: str | None) -> str:
    """One availability state: glyph, then word. An unknown state renders as its bare name."""
    name = state or ""
    if name not in _states.STATES:
        return f'<span class="cx-chip">{html.escape(name)}</span>' if name else ""
    return (f'<span class="cx-chip cx-chip--{name}" title="{html.escape(_why(name))}">'
            f'{_states.state_svg(name)}<span>{name}</span></span>')


def state_glyph_only(state: str | None) -> str:
    """The glyph alone, for a column whose header already says it is a state."""
    name = state or ""
    if name not in _states.STATES:
        return ""
    return (f'<span class="cx-state-glyph cx-state-glyph--{name}" '
            f'title="{html.escape(name + " - " + _why(name))}">{_states.state_svg(name)}</span>')


def verdict(name: str | None) -> str:
    """A recipe verdict, as the contract's three-segment meter."""
    key = name or ""
    if key not in _states.VERDICTS:
        return html.escape(key)
    return (f'<span class="cx-verdict cx-verdict--{key}" '
            f'title="{html.escape(_states.VERDICTS[key].legend)}">'
            f'{_states.verdict_svg(key)}<span>{key}</span></span>')


def kind(name: str | None, *, label: bool = False) -> str:
    """An element kind, drawn with the same mark `diagram.KIND_SHAPE` uses."""
    spec = _states.KINDS.get(name or "", _states.KIND_UNKNOWN)
    text = f'<span>{html.escape(spec.label)}</span>' if label else ""
    return (f'<span class="cx-kind" title="{html.escape(spec.label)}">'
            f'{_states.kind_svg(name or "")}{text}</span>')


def coverage(fraction: str) -> str:
    """A usable fraction with a bar beside it. ``-`` has no bar."""
    text = html.escape(str(fraction))
    have, _, total = str(fraction).partition("/")
    try:
        ratio = int(have) / int(total)
    except (TypeError, ValueError, ZeroDivisionError):
        return f'<span class="cx-coverage">{text}</span>'
    modifier = " cx-coverage--empty" if ratio == 0 else (
        " cx-coverage--partial" if ratio < 1 else "")
    return (f'<span class="cx-coverage{modifier}">'
            f'<span class="cx-coverage__track">'
            f'<span class="cx-coverage__fill" style="width:{ratio * 100:.0f}%"></span></span>'
            f'<span>{text}</span></span>')


def not_probed() -> str:
    return (f'<span class="cx-notprobed" title="{html.escape(NOT_PROBED_TITLE)}">'
            f'{MISSING_NOT_PROBED}</span>')


def state_text(state: str | None) -> str:
    """A state as plain text, for a `<select>` option."""
    return state or ""


def _why(state: str) -> str:
    """The state's explanation from the contract."""
    try:
        return _states.why(state) or _states.STATES[state].legend
    except Exception:  # noqa: BLE001 - a tooltip is never worth a page
        return state


# The legend.
#: Separator between the node-state half and the edge half of `diagram.legend()`. Only the edge
#: half is kept. Without the separator the whole string is shown.
_LEGEND_SPLIT = "  ·  "


def legend_states() -> list[tuple[str, str]]:
    """``(markup, sentence)`` per state, in ladder order, worst first."""
    return [(chip(name), _states.STATES[name].legend) for name in _states.LADDER]


def legend_edges(full: str) -> str:
    """The edge half of `diagram.legend()`."""
    _, sep, edges = full.partition(_LEGEND_SPLIT)
    return edges if sep else full
