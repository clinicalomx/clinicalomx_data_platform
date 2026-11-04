"""The clinicalomx design tokens, as Reflex consumes them. "The Catalogue Card".

The visual authority is ``DESIGN.md`` at the repository root; ``tokens.css`` is that document
made executable; this module is the Python spelling of its token NAMES. Copy to
``<reflex app>/design/tokens.py``. It imports nothing, not even reflex.

EVERY VALUE HERE IS A CSS VARIABLE REFERENCE, NOT A LITERAL::

    rx.box(..., style={"background": tokens.SURFACE_PANEL, "border": tokens.RULE})

``tokens.css`` is the one definition of what a token is worth. A literal here would be a second
definition, and the two would part company the first time somebody adjusted one. The only
literals below are the arguments to ``rx.theme``, which Reflex takes as Python and cannot read
from CSS.

THE ORDER OF THE LAYERS, because getting it wrong produces an app that looks almost right:

1. ``tokens.css`` section 1 vendors the colour primitives. It depends on nothing.
2. ``tokens.css`` sections 2 onward define the semantic tokens and components in plain CSS.
3. ``rx.theme(**THEME)`` points Radix Themes at the SAME two scales, so a Radix component and a
   hand-written rule land on the same values.
4. A component reaches for ``tokens.X`` or a ``cx-`` class name, never a hex value.

Layers 1 and 2 are why the Review app can adopt this language without Reflex: see
:data:`STREAMLIT_THEME`.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------------------
# The Reflex theme. `rx.App(theme=rx.theme(**THEME), stylesheets=STYLESHEETS, style=APP_STYLE)`.
# ---------------------------------------------------------------------------------------
THEME: dict[str, str] = {
    # "inherit" follows the operating system and lets `rx.color_mode.button()` override it.
    # Light is the DEFAULT and the authoritative theme; dark is supported. See DESIGN.md.
    "appearance": "inherit",
    # Jade, chosen by the captain: an instrument green-blue that sits well away from every
    # framework's default blue. It appears on links, focus and selection ONLY. It is NOT the
    # primary action, because jade-9 cannot carry white text (3.15:1); see ACTION_BG.
    "accent_color": "jade",
    # Slate: a cool neutral, which is what keeps a page of dense tables from reading warm.
    "gray_color": "slate",
    # THE CAPTAIN'S ONE EXPLICIT CORRECTION. `none` sets Radix's --radius-factor to 0. Do not
    # move it to "small" to soften a corner; if one corner needs rounding, round that corner
    # and write down why.
    "radius": "none",
    # 100%. Density is controlled by the table's own row tokens, not by a global rescale:
    # 95% shrinks every font and space at once, which makes the measured contrast table and
    # the 24px target-size floor harder to reason about.
    "scaling": "100%",
    # Solid, not translucent. A translucent panel over a table is a legibility cost paid for a
    # decorative effect, and this app is mostly tables.
    "panel_background": "solid",
}

#: The faces. A URL entry in `rx.App(stylesheets=)` is passed through untouched, so this one
#: works whichever delivery the app picks below.
FONT_STYLESHEET = (
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600"
    "&family=IBM+Plex+Serif:wght@500;600"
    "&family=IBM+Plex+Mono:wght@400;500&display=swap")

#: DELIVERY A, for an app that copies `tokens.css` into its own `assets/`. A non-URL entry in
#: `stylesheets` is resolved under the PROJECT assets directory, so this only works when the file
#: is genuinely there. Order matters: the faces have to exist before tokens.css names them.
STYLESHEETS: list[str] = [FONT_STYLESHEET, "/tokens.css"]


def tokens_css() -> str:
    """DELIVERY B, for an app whose ``tokens.css`` ships INSIDE A WHEEL rather than in `assets/`.

    Inline the return value as a ``<style>`` element in the document HEAD::

        app = rx.App(
            theme=rx.theme(**tokens.THEME),
            stylesheets=[tokens.FONT_STYLESHEET],
            head_components=[rx.el.style(tokens.tokens_css())],
            style=tokens.APP_STYLE,
        )

    WHY THIS EXISTS. `rx.App(stylesheets=)` resolves a non-URL entry under the project's own
    `assets/` directory, and `rx.asset` - the documented bridge for a file that lives in a package
    - does not compose with it: it returns a `?v=` cache-buster behind the `frontend_path` prefix
    and the stylesheets resolver strips neither, so the build asks for
    `assets/build/external/...css?v=...` and fails. Found by the Reflex port, which serves two
    projects from one wheel and so cannot copy the file into either one's assets.

    HEAD, NOT BODY, and that is the whole requirement. A `<style>` in the body paints the page
    once unstyled first, and this system's first paint is a dense table; a flash of it at browser
    defaults is worse than a slower one. `head_components` is the placement that guarantees it.

    The cost is one un-cached ~30 KB inline block per page load, which an internal app on a
    control-plane VM does not notice, against keeping ONE copy of the stylesheet for two consumers.
    That trade is the right way round; do not "fix" it by forking the file.
    """
    from pathlib import Path

    return (Path(__file__).with_name("tokens.css")).read_text(encoding="utf-8")


#: Radix step per named space token, for a Reflex prop that takes a SCALE STEP rather than a CSS
#: length - `rx.vstack(spacing=...)` and its siblings. The two scales agree by construction
#: because `--cx-space-N` IS Radix's step N, and they agree NUMERICALLY only while
#: `THEME["scaling"]` is "100%", because Radix multiplies its step values by the scaling factor
#: and a CSS length written by this system does not move. That invariant is the reason `scaling`
#: is not a density lever here, and the Reflex port asserts it in a test. If scaling ever changes,
#: either every `--cx-space-*` literal moves with it or this map stops being true; there is no
#: third option, so change neither casually.
SPACING_STEP: dict[str, str] = {
    "SPACE_1": "1", "SPACE_2": "2", "SPACE_3": "3",
    "SPACE_4": "4", "SPACE_5": "5", "SPACE_6": "6",
}

#: The same language for the Streamlit Review app, which adopts it next. Goes in that app's
#: `.streamlit/config.toml` under `[theme]`, and `tokens.css` is injected beside it. These are
#: the only literals in this module, and each is a copy of a primitive in `tokens.css` section 1;
#: Streamlit's theme block takes hex and will not read a custom property.
STREAMLIT_THEME: dict[str, str] = {
    "base": "light",
    "backgroundColor": "#fcfcfd",           # --cx-gray-1
    "secondaryBackgroundColor": "#f9f9fb",  # --cx-gray-2
    "textColor": "#1c2024",                 # --cx-gray-12
    "primaryColor": "#29a383",              # --cx-accent-9 (jade), for Streamlit's own accents
    "font": "sans serif",
}

# ---------------------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------------------
FONT_SERIF = "var(--cx-font-serif)"  # headings only: the page title and the section head
FONT_SANS = "var(--cx-font-sans)"    # the interface
FONT_MONO = "var(--cx-font-mono)"    # anything compared character by character

TEXT_MICRO = "var(--cx-text-micro)"      # 12px - legend, caption, table meta
TEXT_DENSE = "var(--cx-text-dense)"      # 12px - dense table body, chips
TEXT_BODY = "var(--cx-text-body)"        # 14px - interface default
TEXT_LEAD = "var(--cx-text-lead)"        # 16px - readout value
TEXT_TITLE = "var(--cx-text-title)"      # 18px - panel or slot title
TEXT_SECTION = "var(--cx-text-section)"  # 24px - tab-level heading (serif)
TEXT_PAGE = "var(--cx-text-page)"        # 28px - page title, once (serif)

WEIGHT_REGULAR = "var(--cx-weight-regular)"
WEIGHT_MEDIUM = "var(--cx-weight-medium)"
WEIGHT_STRONG = "var(--cx-weight-strong)"

# ---------------------------------------------------------------------------------------
# Space, shape
# ---------------------------------------------------------------------------------------
SPACE_HAIR = "var(--cx-space-hair)"  #  2px, a dense table cell and nothing else
SPACE_1 = "var(--cx-space-1)"        #  4px
SPACE_2 = "var(--cx-space-2)"        #  8px
SPACE_3 = "var(--cx-space-3)"        # 12px
SPACE_4 = "var(--cx-space-4)"        # 16px
SPACE_5 = "var(--cx-space-5)"        # 24px
SPACE_6 = "var(--cx-space-6)"        # 32px

RADIUS = "var(--cx-radius)"              # 0. There is one.
RADIUS_ROUND = "var(--cx-radius-round)"  # radio indicator and busy spinner ONLY

# ---------------------------------------------------------------------------------------
# Surface, text, rule
# ---------------------------------------------------------------------------------------
SURFACE_PAGE = "var(--cx-surface-page)"
SURFACE_PANEL = "var(--cx-surface-panel)"
SURFACE_INSET = "var(--cx-surface-inset)"
SURFACE_SELECTED = "var(--cx-surface-selected)"

TEXT_PRIMARY = "var(--cx-text-primary)"
TEXT_MUTED = "var(--cx-text-muted)"
TEXT_ACCENT = "var(--cx-text-accent)"
TEXT_INVERSE = "var(--cx-text-inverse)"

RULE = "var(--cx-rule)"                      # structure: table rows, panel edges
RULE_STRONG = "var(--cx-rule-strong)"        # a boundary between two ideas
BORDER_CONTROL = "var(--cx-border-control)"  # anything operable; 3:1 verified
FOCUS_RING = "var(--cx-focus-ring)"

#: The primary action. INK, not the accent, and that is measured rather than tasteful: white on
#: jade-9 is 3.15:1 and fails AA, while the neutral at full strength is 15.98:1 light and
#: 16.25:1 dark with one rule and no mode-specific inversion.
ACTION_BG = "var(--cx-action-bg)"
ACTION_FG = "var(--cx-action-fg)"

ELEVATION_FLAT = "var(--cx-elevation-flat)"    # none. Use a rule.
ELEVATION_FLOAT = "var(--cx-elevation-float)"  # popover, select menu, dialog. Nothing else.

# ---------------------------------------------------------------------------------------
# Density
# ---------------------------------------------------------------------------------------
ROW_H = "var(--cx-row-h)"
CELL_PX = "var(--cx-cell-px)"
CELL_PY = "var(--cx-cell-py)"
TABLE_MAX_H = "var(--cx-table-max-h)"

#: Row heights for a table that sets its own. `compact` is the 120-row setting and is what the
#: corpus, coverage, element and layout tables use. `comfortable` is for a table under about 15
#: rows where the air costs nothing: the Jobs run list, a recipe's element list.
#: `compact` is 24px, not 22px, because WCAG 2.2 (2.5.8) puts the minimum target size
#: at 24, so a control inside a compact row needs no special case.
DENSITY = {"compact": "24px", "default": "26px", "comfortable": "32px"}

CONTENT_MAX_W = "var(--cx-content-max-w)"  # 1440px
SIDEBAR_W = "var(--cx-sidebar-w)"          # 268px

# ---------------------------------------------------------------------------------------
# Global style for rx.App(style=...). Everything else is a class name or a token above.
# ---------------------------------------------------------------------------------------
APP_STYLE: dict = {
    "font_family": FONT_SANS,
    "font_size": TEXT_BODY,
    "background": SURFACE_PAGE,
    "color": TEXT_PRIMARY,
    # Every number in this app sits in a column with other numbers. Set once here rather than
    # remembered at thirty call sites.
    "font_variant_numeric": "tabular-nums",
}
