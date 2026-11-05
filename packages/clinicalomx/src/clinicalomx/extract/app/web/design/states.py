"""The Build app's state vocabulary."""
from __future__ import annotations

from dataclasses import dataclass

#: Ladder order, least usable first. The same order as ``availability._RANK``, and the order the
#: legend prints in, so the legend teaches the ladder rather than listing seven unrelated things.
LADDER: tuple[str, ...] = (
    "undeclared", "unlinked", "denied", "dangling", "missing", "unplaceable", "present")


@dataclass(frozen=True)
class StateSpec:
    """One availability state as it is drawn.

    ``scale`` names a colour scale in the token layer. Every colour a chip uses is derived from it
    by step, so there are no loose hex values: background step 3, border step 6, glyph and word
    step 12. Those steps are measured, not guessed; DESIGN.md carries the table.

    ``tinted`` is the whole colour policy in one boolean. False means the chip draws no background
    and no border, so the row stays quiet. See the module docstring.
    """

    name: str
    rank: int
    scale: str
    tinted: bool
    glyph: str  # inner SVG markup, 14x14 viewBox, strokes and fills in currentColor
    #: Four to eight words for the legend. The FULL explanation is `availability._WHY`; this is
    #: the line under a legend swatch, where that one is a paragraph.
    legend: str


_SQ = 'x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.5"'

STATES: dict[str, StateSpec] = {
    "undeclared": StateSpec(
        "undeclared", 0, "gray", False,
        # No edges at all, only corner ticks: there is not even a frame, because nothing declares
        # that this slot should exist. It is the one state drawn without a box, deliberately.
        '<path d="M2.5 4.75V2.5H4.75M9.25 2.5H11.5V4.75M11.5 9.25V11.5H9.25M4.75 11.5H2.5V9.25" '
        'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="square"/>',
        "no frame: nothing declares this role"),
    "unlinked": StateSpec(
        "unlinked", 1, "gray", False,
        f'<rect {_SQ} stroke-dasharray="1.75 1.5"/>',
        "the slot is declared and empty"),
    "denied": StateSpec(
        "denied", 2, "red", True,
        f'<rect {_SQ}/><path d="M1 11L11 1" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="square"/>',
        "barred: governance forbids the read"),
    "dangling": StateSpec(
        "dangling", 3, "orange", True,
        f'<rect {_SQ}/><path d="M4.25 4.25L7.75 7.75M7.75 4.25L4.25 7.75" fill="none" '
        'stroke="currentColor" stroke-width="1.25"/>',
        "the pointer goes nowhere"),
    "missing": StateSpec(
        "missing", 4, "orange", True,
        f'<rect {_SQ}/>',
        "a registered frame with no bytes"),
    "unplaceable": StateSpec(
        "unplaceable", 5, "amber", True,
        f'<rect {_SQ}/><rect x="2.5" y="2.5" width="3.5" height="7" fill="currentColor"/>',
        "half of what you need: no position"),
    "present": StateSpec(
        # Ink, not green. The only fully filled mark in the set, which is what "complete" looks
        # like when colour has been reserved for things that need doing.
        "present", 6, "gray", False,
        '<rect x="1.75" y="1.75" width="8.5" height="8.5" fill="currentColor"/>',
        "readable and placeable"),
}

#: The four that colour a row. Derived, never restated: a fifth tinted state is added by flipping
#: `tinted`, and every table, legend and print rule picks it up.
TINTED: tuple[str, ...] = tuple(s for s in LADDER if STATES[s].tinted)


@dataclass(frozen=True)
class VerdictSpec:
    """A recipe verdict, drawn as a three-segment meter rather than a square.

    Deliberately a DIFFERENT shape family from :class:`StateSpec`. A verdict summarises many
    states, so drawing it with the same marks would invite a reader to compare one recipe's
    verdict against one role's state as though they were the same measurement.
    """

    name: str
    filled: int
    scale: str
    legend: str


VERDICTS: dict[str, VerdictSpec] = {
    "blocked": VerdictSpec("blocked", 0, "red", "no unit of this recipe can be built"),
    "degraded": VerdictSpec("degraded", 2, "amber", "buildable, with something absent"),
    # Ink, for the same reason `present` is: a full meter needs no colour to read as full.
    "ready": VerdictSpec("ready", 3, "gray", "every declared element resolves"),
}

#: Verdict order for sorting and for the legend: worst first, same direction as LADDER.
VERDICT_ORDER: tuple[str, ...] = ("blocked", "degraded", "ready")


@dataclass(frozen=True)
class KindSpec:
    """An element kind, drawn as the SAME mark the DOT diagram gives it.

    ``clinicalomx.extract.app.diagram.KIND_SHAPE`` picks a graphviz shape per kind, and the table
    icon here is that shape drawn by hand. One vocabulary, two renderers: a reader who learns that
    a hexagon is geometry in the picture reads the hexagon in the table without being told again.
    """

    name: str
    dot_shape: str  # what diagram.KIND_SHAPE gives this kind; keep the two in step
    glyph: str  # inner SVG markup, 14x14 viewBox
    label: str


KINDS: dict[str, KindSpec] = {
    "image": KindSpec(
        "image", "box",
        '<rect x="1.5" y="3" width="11" height="8" fill="none" stroke="currentColor" '
        'stroke-width="1.25"/><path d="M1.5 6.5H12.5M1.5 9H12.5" stroke="currentColor" '
        'stroke-width="1" opacity="0.55"/>',
        "Images"),
    "labels": KindSpec(
        "labels", "box3d",
        '<rect x="1.25" y="1.75" width="9" height="8" fill="none" stroke="currentColor" '
        'stroke-width="1.25" opacity="0.5"/><rect x="3.75" y="4.25" width="9" height="8" '
        'fill="none" stroke="currentColor" stroke-width="1.25"/>',
        "Labels"),
    "shapes": KindSpec(
        "shapes", "hexagon",
        '<path d="M4.25 2H9.75L12.5 7L9.75 12H4.25L1.5 7Z" fill="none" stroke="currentColor" '
        'stroke-width="1.25" stroke-linejoin="miter"/>',
        "Shapes"),
    "table": KindSpec(
        "table", "note",
        '<path d="M2 1.75H9.5L12 4.25V12.25H2Z" fill="none" stroke="currentColor" '
        'stroke-width="1.25" stroke-linejoin="miter"/><path d="M9.5 1.75V4.25H12" '
        'fill="none" stroke="currentColor" stroke-width="1.25"/><path d="M4 7.25H10M4 9.5H10" '
        'stroke="currentColor" stroke-width="1" opacity="0.55"/>',
        "Tables"),
    "points": KindSpec(
        "points", "ellipse",
        '<rect x="2" y="8.5" width="2.5" height="2.5" fill="currentColor"/>'
        '<rect x="5.75" y="3" width="2.5" height="2.5" fill="currentColor"/>'
        '<rect x="9.5" y="7" width="2.5" height="2.5" fill="currentColor"/>',
        "Points"),
    "attrs": KindSpec(
        "attrs", "box",
        '<path d="M2 4H7M2 7H12M2 10H9" stroke="currentColor" stroke-width="1.25" '
        'stroke-linecap="square"/>',
        "Attributes"),
}

#: What a kind-less candidate is drawn with. `observe` cannot classify every zarr or parquet, and
#: a guessed icon is a claim the layer refused to make - so the mark says "unclassified" out loud.
KIND_UNKNOWN = KindSpec(
    "", "box",
    '<rect x="2" y="2" width="10" height="10" fill="none" stroke="currentColor" '
    'stroke-width="1.25" stroke-dasharray="2 1.75"/>',
    "Unclassified")


# ---------------------------------------------------------------------------------------
# rendering helpers - strings, so the caller wraps them however its framework likes
# ---------------------------------------------------------------------------------------
def state_svg(state: str, size: int = 12) -> str:
    """The state glyph as a standalone ``<svg>`` string, coloured by ``currentColor``.

    ``shape-rendering="crispEdges"`` is not cosmetic: every mark here is axis-aligned at 12px, and
    without it a 1.5px stroke lands on a half pixel and blurs into a 2px grey smear, which is
    exactly the difference between a solid frame and a dashed one.
    """
    return (f'<svg class="cx-glyph" width="{size}" height="{size}" viewBox="0 0 14 14" '
            f'shape-rendering="crispEdges" aria-hidden="true" focusable="false">'
            f'{STATES[state].glyph}</svg>')


def verdict_svg(verdict: str, size: int = 14) -> str:
    """The verdict meter: three segments, ``filled`` of them solid, plus a bar when blocked."""
    spec = VERDICTS[verdict]
    parts = []
    for i in range(3):
        fill = "currentColor" if i < spec.filled else "none"
        parts.append(f'<rect x="{1.5 + i * 4.0}" y="2.5" width="2.75" height="9" fill="{fill}" '
                     f'stroke="currentColor" stroke-width="1"/>')
    if spec.filled == 0:
        parts.append('<path d="M1 11.5L13 2.5" stroke="currentColor" stroke-width="1.75" '
                     'stroke-linecap="square"/>')
    return (f'<svg class="cx-glyph" width="{size}" height="{size}" viewBox="0 0 14 14" '
            f'shape-rendering="crispEdges" aria-hidden="true" focusable="false">'
            f'{"".join(parts)}</svg>')


def kind_svg(kind: str, size: int = 14) -> str:
    """The kind glyph. An unknown or empty kind gets the dashed box, never a guessed icon."""
    spec = KINDS.get(kind or "", KIND_UNKNOWN)
    return (f'<svg class="cx-glyph" width="{size}" height="{size}" viewBox="0 0 14 14" '
            f'aria-hidden="true" focusable="false">{spec.glyph}</svg>')


def why(state: str) -> str:
    """The real explanation, read from the availability module rather than restated here.

    ONE import site on purpose. ``_WHY`` is private today; when it is made public this function is
    the only line that changes, and nothing in the UI has to be found and edited.
    """
    from clinicalomx.extract.availability import _WHY

    return _WHY.get(state, "")
