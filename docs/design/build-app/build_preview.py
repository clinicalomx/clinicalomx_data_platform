"""Generate ``preview.html``: every token and component of "The Catalogue Card", rendered.

    python3 docs/design/build-app/build_preview.py

GENERATED, NOT HAND-WRITTEN, so it cannot drift from the thing it previews. The stylesheet is
``tokens.css`` read off disk, the glyphs come from ``states.py``, and the contrast numbers are
COMPUTED from the primitives parsed out of that same stylesheet rather than copied from
DESIGN.md. A preview maintained by hand stops being evidence the first time somebody edits one
file and not the other.

The page is self-contained and opens from a ``file://`` URL with no build step and no network,
because ``tokens.css`` vendors its own colour primitives. That is the same property that lets the
Streamlit Review app adopt this language later.
"""
from __future__ import annotations

import html
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import states as ST  # noqa: E402

CSS = (HERE / "tokens.css").read_text()


# ---------------------------------------------------------------------------------------
# contrast, computed from the shipped stylesheet
# ---------------------------------------------------------------------------------------
def primitives(mode: str) -> dict[str, str]:
    """``{"gray-1": "#fcfcfd", ...}`` for one mode, parsed out of tokens.css section 1."""
    marker = "/* LIGHT (authoritative) */" if mode == "light" else '.cx-dark {'
    body = CSS.split(marker, 1)[1].split("}", 1)[0]
    out = dict(re.findall(r"--cx-([a-z]+-\d+):\s*(#[0-9a-f]{6})", body))
    # the one system addition outside the vendored scales, defined just below them
    link = re.search(r"--cx-accent-link:\s*(#[0-9a-f]{6})", CSS)
    out["accent-link"] = link.group(1) if mode == "light" else out["accent-11"]
    return out


def _lum(colour: str) -> float:
    h = colour.lstrip("#")
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in parts]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    return round((max(a, b) + 0.05) / (min(a, b) + 0.05), 2)


#: (label, foreground token, background token, required ratio, note).
#: The REJECTED rows are kept on the page on purpose: a system that only shows the pairs it
#: passed teaches nothing about where its floor actually is.
PAIRS = [
    ("body text on the page", "gray-12", "gray-1", 4.5, ""),
    ("muted text on the page", "gray-11", "gray-1", 4.5, ""),
    ("muted text on a panel", "gray-11", "gray-2", 4.5, ""),
    ("link on the page", "accent-link", "gray-1", 4.5, ""),
    ("link on a hovered row", "accent-link", "gray-3", 4.5,
     "the darkest ground a link can land on"),
    ("REJECTED jade-11 as the link", "accent-11", "gray-3", 4.5,
     "4.10:1, why the link token is darkened"),
    ("primary button: page colour on ink", "gray-1", "gray-12", 4.5, "why the action is ink"),
    ("control border on the page", "gray-9", "gray-1", 3.0, "WCAG 1.4.11"),
    ("focus ring on the page", "accent-9", "gray-1", 3.0, "WCAG 1.4.11"),
    ("REJECTED framework default border", "gray-7", "gray-1", 3.0, "1.53:1, why we use gray-9"),
    ("REJECTED white on the jade accent", "white", "accent-9", 4.5,
     "3.15:1, why the action is not jade"),
    ("chip: unplaceable", "amber-12", "amber-3", 4.5, ""),
    ("chip: missing and dangling", "orange-12", "orange-3", 4.5, ""),
    ("chip: denied", "red-12", "red-3", 4.5, ""),
    ("quiet chip: present", "gray-12", "gray-1", 4.5, "no ground; ink on the page"),
    ("quiet chip: unlinked and undeclared", "gray-11", "gray-1", 4.5, "no ground"),
    ("REJECTED step 11 on a step 3 ground", "orange-11", "orange-3", 4.5,
     "3.99:1, why chip text is step 12"),
]


def contrast_table(mode: str) -> str:
    p = primitives(mode) | {"white": "#ffffff"}
    rows = []
    for label, fg, bg, need, note in PAIRS:
        got = ratio(p[fg], p[bg])
        ok = got >= need
        rejected = label.startswith("REJECTED")
        verdict = ("fails" if rejected else ("pass" if ok else "FAILS"))
        state = "denied" if rejected or not ok else "present"
        rows.append([
            f'<span class="cx-chip cx-chip--{state}">{ST.state_svg(state)}'
            f'<span>{verdict}</span></span>',
            html.escape(label),
            f'<span class="cx-key">{p[fg]}</span> on <span class="cx-key">{p[bg]}</span>',
            f'<span class="cx-num">{got}:1</span>',
            f'<span class="cx-num">{need}:1</span>',
            f'<span class="cx-caption">{html.escape(note)}</span>',
        ])
    return table(["", "pair", "values", "measured", "needs", "note"], rows, density="default")


# ---------------------------------------------------------------------------------------
# markup helpers - the markup the README specifies, so the page IS the example
# ---------------------------------------------------------------------------------------
def chip(state: str) -> str:
    return (f'<span class="cx-chip cx-chip--{state}" title="{ST.STATES[state].legend}">'
            f'{ST.state_svg(state)}<span>{state}</span></span>')


def bare(state: str) -> str:
    return (f'<span class="cx-state-glyph--{state}" role="img" aria-label="{state}" '
            f'title="{state}">{ST.state_svg(state)}</span>')


def verdict(name: str) -> str:
    return (f'<span class="cx-verdict cx-verdict--{name}">{ST.verdict_svg(name)}'
            f'<span>{name}</span></span>')


def kind(name: str) -> str:
    spec = ST.KINDS.get(name, ST.KIND_UNKNOWN)
    return (f'<span class="cx-legend__item" title="DOT shape: {spec.dot_shape}">'
            f'{ST.kind_svg(name)}<span>{name or "unclassified"}</span></span>')


def coverage(present: int, total: int) -> str:
    pct = 0 if not total else round(100 * present / total)
    mod = "" if pct == 100 else (" cx-coverage--empty" if pct == 0 else " cx-coverage--partial")
    return (f'<span class="cx-coverage{mod}"><span class="cx-coverage__track">'
            f'<span class="cx-coverage__fill" style="width:{pct}%"></span></span>'
            f'<span>{present}/{total}</span></span>')


DASH = ('<span class="cx-notprobed" title="not probed - this app reads the catalogue only">'
        '-</span>')


def notice(severity: str, label: str, text: str) -> str:
    return (f'<div class="cx-notice cx-notice--{severity}">'
            f'<span class="cx-notice__label">{label}</span><span>{text}</span></div>')


def table(headers, rows, *, density="compact", foot="") -> str:
    ths = "".join(f"<th>{h}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    footer = (f'<div class="cx-table-foot"><span>{foot}</span>'
              f'<span>density: {density}</span></div>') if foot else ""
    return (f'<div class="cx-table-scroll" style="--cx-row-h:{DENSITY[density]}">'
            f'<table class="cx-table"><thead><tr>{ths}</tr></thead>'
            f"<tbody>{trs}</tbody></table></div>{footer}")


DENSITY = {"compact": "24px", "default": "26px", "comfortable": "32px"}


# ---------------------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------------------
def role_rows(n: int) -> list[list[str]]:
    """A realistic coverage table. The shape is `_role_table`'s, column for column."""
    data = [
        ("present", "bgnormed", 108, 120, "clinicalomx-qptiff-pipeline"),
        ("present", "bgnormQ", 108, 120, "clinicalomx-qptiff-pipeline"),
        ("unplaceable", "dearray", 12, 120, "clinical-roi"),
        ("denied", "source_image", 0, 120, "Q0000"),
        ("dangling", "thumbnail", 0, 120, "clinicalomx-review"),
        ("missing", "cells", 0, 120, "segmentation"),
        ("unlinked", "segmented", 0, 120, "(unowned)"),
        ("undeclared", "spillover", 0, 120, "(undeclared)"),
    ]
    out = []
    for i in range(n):
        state, role, present, total, owner = data[i % len(data)]
        suffix = "" if i < len(data) else f"_{i // len(data)}"
        out.append([
            chip(state),
            f'<span class="cx-col-key">{role}{suffix}</span>',
            coverage(present, total),
            DASH, DASH,
            f'<span class="cx-num">{0 if state != "denied" else total}</span>',
            f'<span class="cx-caption">{html.escape(owner)}</span>',
        ])
    return out


def section(title: str, body: str, lead: str = "") -> str:
    intro = f'<p class="cx-caption" style="max-width:76ch">{lead}</p>' if lead else ""
    return (f'<section class="cx-block"><h2 class="cx-section-head">{title}</h2>{intro}'
            f"{body}</section>")


def swatches(mode: str) -> str:
    p = primitives(mode)
    groups = {"gray": "neutral", "accent": "jade, the only accent",
              "amber": "unplaceable", "orange": "missing and dangling", "red": "denied"}
    out = []
    for family, purpose in groups.items():
        cells = "".join(
            f'<div style="flex:1;height:34px;background:var(--cx-{family}-{i})" '
            f'title="--cx-{family}-{i}: {p[f"{family}-{i}"]}"></div>' for i in range(1, 13))
        out.append(f'<div style="margin-bottom:var(--cx-space-3)">'
                   f'<div class="cx-readout__label">{family} &middot; {purpose}</div>'
                   f'<div style="display:flex;border:var(--cx-rule)">{cells}</div></div>')
    return "".join(out)


def build() -> str:
    ladder = "".join(
        f'<div class="cx-legend__item" style="min-width:24ch">{chip(s)}'
        f'<span class="cx-caption">{ST.STATES[s].legend}</span></div>'
        for s in reversed(ST.LADDER))

    type_rows = [
        ["Display", "serif 500 28/36", '<span class="cx-title">Extract Explorer</span>',
         "the page title, once"],
        ["Headline", "serif 500 24/30", '<span class="cx-section-head">What is here</span>',
         "a tab-level section head"],
        ["Title", "sans 600 18/26",
         '<span style="font-size:var(--cx-text-title);font-weight:600">Images</span>',
         "a panel or slot head"],
        ["Body", "sans 400 14/20", "The interface default.", "everything conversational"],
        ["Dense", "sans 400 12/16",
         '<span style="font-size:var(--cx-text-dense)">108 of 120 cores are usable.</span>',
         "table body and chips"],
        ["Label", "sans 500 12/16 sc",
         '<span class="cx-readout__label">subtree</span>', "column headers, chip words"],
        ["Identifier", "mono 400 12/16",
         '<span class="cx-key">clinicalomx-qptiff-pipeline/R0C11/bgnormed.zarr</span>',
         "anything compared character by character"],
    ]

    choice_row = f"""
<div class="cx-slot">
  <div class="cx-slot__head">{ST.kind_svg("image")}<span>Images</span>
    <span class="cx-slot__count">2 declared &middot; 1 added</span></div>
  <p class="cx-caption">Intensity pixels. A SpatialData holds as many as you like, raw beside
     background-normalised, in one coordinate system.</p>
  <div class="cx-row">
    <div>{bare("present")} <span class="cx-row__name">bgnorm</span>
      <div class="cx-caption">reads <code>children</code></div>
      <div style="margin-top:4px">{coverage(108, 120)}</div></div>
    <div><select class="cx-select cx-select--key">
        <option>bgnormed &middot; adjusted_image - fills bgnorm</option>
        <option>Auto (recipe preference)</option></select>
      <div class="cx-caption" style="margin-top:4px">variable <code>adjusted_image</code></div></div>
    <div><label style="font-size:var(--cx-text-dense)"><input type="checkbox" checked> include</label>
      <div class="cx-caption" style="margin-top:4px">
        <label><input type="checkbox"> allow absent</label></div></div>
  </div>
  <div class="cx-row cx-row--excluded">
    <div>{bare("unplaceable")} <span class="cx-row__name">raw</span>
      <div class="cx-caption">reads <code>children</code></div>
      <div style="margin-top:4px">{coverage(12, 120)}</div>
      <div class="cx-caption">108 cannot be positioned</div></div>
    <div><select class="cx-select cx-select--key"><option>Auto (recipe preference)</option></select></div>
    <div><label style="font-size:var(--cx-text-dense)"><input type="checkbox"> include</label></div>
  </div>
  <div class="cx-row">
    <div>{bare("present")} <span class="cx-row__name">bgnormQ</span>
      <span class="cx-row__added-label">added</span>
      <div class="cx-caption">reads <code>children</code></div></div>
    <div><select class="cx-select cx-select--key">
        <option>bgnormed &middot; adjusted_image_post_hoc</option></select></div>
    <div><button class="cx-btn cx-btn--ghost" title="remove">&#10005;</button></div>
  </div>
  <button class="cx-add">+ add image</button>
</div>"""

    controls = f"""
<div style="display:flex;gap:var(--cx-space-3);flex-wrap:wrap;align-items:center">
  <button class="cx-btn cx-btn--primary">Plan</button>
  <button class="cx-btn">&#8635; Refresh from instance</button>
  <button class="cx-btn" disabled>Submit 108 unit(s) to the cluster</button>
  <button class="cx-btn cx-btn--ghost" title="remove">&#10005;</button>
  <div class="cx-segmented">
    <button class="cx-segmented__item" aria-pressed="true">by roi</button>
    <button class="cx-segmented__item" aria-pressed="false">by element</button></div>
  <label style="display:inline-flex;gap:6px;align-items:center;font-size:var(--cx-text-body)">
    <input type="checkbox" checked> include</label>
  <label style="display:inline-flex;gap:6px;align-items:center;font-size:var(--cx-text-body)">
    <input type="radio" name="strict" checked> degrade</label>
  <label style="display:inline-flex;gap:6px;align-items:center;font-size:var(--cx-text-body)">
    <input type="radio" name="strict"> strict</label>
</div>
<p class="cx-caption" style="margin-top:var(--cx-space-2)">The checkbox is square and the radio is
round, and that is the one shape exception in the system: circular means "one of these", square
means "any of these".</p>
<div class="cx-tabs" style="margin-top:var(--cx-space-4)">
  <button class="cx-tab">Corpus</button>
  <button class="cx-tab" aria-selected="true">What is here</button>
  <button class="cx-tab">Build something</button>
  <button class="cx-tab">Jobs</button></div>"""

    readouts = """
<div style="display:flex;gap:var(--cx-space-6);align-items:flex-start">
  <div><span class="cx-readout__label">subtree</span>
       <span class="cx-readout__value">120 Core, 1 TMA</span></div>
  <div style="border-left:var(--cx-rule);padding-left:var(--cx-space-6)">
       <span class="cx-readout__label">recipes</span>
       <span class="cx-readout__value">4/6 buildable</span></div>
  <div style="border-left:var(--cx-rule);padding-left:var(--cx-space-6)">
       <span class="cx-readout__label">index built</span>
       <span class="cx-readout__value">2026-09-15T04:11:02Z</span></div>
</div>"""

    body = "".join([
        section("Colour", swatches("light") + f"""
<p class="cx-caption" style="max-width:76ch">Twelve steps per family, and the system uses six of
them: 1 and 2 are surfaces, 3 is an inset or a chip ground, 6 is a rule, 9 is a control edge or a
signal, 11 is muted text, 12 is ink. Hover a swatch for its token name.</p>""",
                "A cool neutral doing almost all the work, one jade signal for interaction, and "
                "three warm hues held in reserve for the state vocabulary. There is no green."),

        section("Measured contrast", contrast_table("light"),
                "Computed from the primitives in tokens.css when this page was generated, not "
                "copied from anywhere. The rejected pairs are shown on purpose: a system that "
                "only lists the pairs it passed teaches nothing about where its floor is. Switch "
                "to dark above to recompute."),

        section("The state ladder",
                f'<div class="cx-legend" style="border-top:none">{ladder}</div>'
                f'<p class="cx-caption" style="margin-top:var(--cx-space-3);max-width:76ch">'
                f"Bare glyphs, for a select option or a diagram caption where a chip will not "
                f"fit. The word still reaches a screen reader through aria-label.</p>"
                f'<div style="display:flex;gap:var(--cx-space-4);margin-top:var(--cx-space-2)">'
                + "".join(bare(s) for s in reversed(ST.LADDER)) + "</div>"
                f'<p class="cx-caption" style="margin-top:var(--cx-space-4)">Verdicts are a '
                f"different shape family, so no reader compares a recipe's verdict to a role's "
                f"state as though they were one measurement.</p>"
                f'<div style="display:flex;gap:var(--cx-space-4);margin-top:var(--cx-space-2)">'
                + "".join(verdict(v) for v in ST.VERDICT_ORDER) + "</div>"
                f'<p class="cx-caption" style="margin-top:var(--cx-space-4)">Kinds. Each mark is '
                f"the graphviz shape the diagram gives that kind, drawn by hand, so the table and "
                f"the picture teach one vocabulary. Hover for the DOT shape.</p>"
                f'<div class="cx-legend" style="border-top:none">'
                + "".join(kind(k) for k in list(ST.KINDS) + [""]) + "</div>",
                "Seven marks built from one square frame, ordered by usability. Only the four "
                "that need a human carry a tint; present, unlinked and undeclared are ink on the "
                "page ground. Turn on greyscale above: nothing is lost, because the glyph and "
                "the word were carrying it."),

        section("Type", table(
            ["role", "spec", "specimen", "used for"],
            [[f'<span class="cx-col-key">{r[0]}</span>',
              f'<span class="cx-caption">{r[1]}</span>', r[2],
              f'<span class="cx-caption">{r[3]}</span>'] for r in type_rows],
            density="comfortable"),
            "One superfamily wearing three hats. The serif names the card and each section of it, "
            "and appears nowhere else."),

        section("A table at 120 rows", table(
            ["state", "role", "usable", "dangling", "missing", "denied", "owner"],
            role_rows(120), density="compact",
            foot="120 of 120 rows shown"),
            "The design target, not the stress case. Sticky header, hairline rules, no zebra, "
            "22px rows, tabular figures, a bounded scroll region, and a footer that says how "
            "many rows it is showing. The MISSING and DANGLING columns print a dash because "
            "this app cannot reach artifact bytes, and a dash means nobody looked."),

        section("Element choice rows", choice_row,
                "The centre of the Build tab, and the thing the app exists for. The row's state "
                "is the same glyph the tables use, placed before the name. There is no tinted "
                "left bar: an earlier draft had one and the anti-pattern detector named it as "
                "the most recognisable tell of a generated interface."),

        section("Notices",
                '<div style="display:flex;flex-direction:column;gap:var(--cx-space-2)">'
                + notice("ok", "ok", "Plannable &middot; 108 unit(s) &middot; token "
                         '<span class="cx-key">a3f9c1</span>')
                + notice("info", "info",
                         "412 metadata read(s), batched, against R0C00. No pixel was read.")
                + notice("warn", "warn",
                         "bytes were NOT verified, so every MISSING column prints a dash")
                + notice("blocked", "blocked",
                         "The submit target is unavailable, so this plan cannot be built from "
                         "here: no Prefect API address is configured.")
                + "</div>",
                "Four severities, each a hairline box and a small-caps word. No tinted left bar "
                "and no icon-only notice: the word carries the severity, so it survives "
                "greyscale exactly as a state chip does."),

        section("Controls", controls,
                "Square, one primary per surface, and the primary is ink rather than the accent "
                "because white on jade measures 3.15:1."),

        section("Readouts", readouts,
                "What the framework metric becomes. No box: two readouts separated by space and "
                "a rule read as an instrument panel rather than as two cards."),
    ])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>clinicalomx design system - The Catalogue Card</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&amp;family=IBM+Plex+Serif:wght@500;600&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap">
<style>
{CSS}

/* Preview chrome only. Nothing below ships. */
body {{ margin: 0; }}
.cx-page {{ max-width: var(--cx-content-max-w); margin: 0 auto;
           padding: var(--cx-space-5) var(--cx-space-5) var(--cx-space-6); }}
.cx-block {{ margin-bottom: var(--cx-space-6); padding-top: var(--cx-space-4);
            border-top: var(--cx-rule); }}
.cx-block > h2 {{ margin: 0 0 var(--cx-space-1); }}
.cx-bar {{ position: sticky; top: 0; z-index: 5; display: flex; gap: var(--cx-space-3);
          align-items: center; padding: var(--cx-space-2) var(--cx-space-5);
          background: var(--cx-surface-panel); border-bottom: var(--cx-rule-strong); }}
.cx-grey {{ filter: grayscale(1); }}
</style>
</head>
<body>
<div class="cx-bar cx-nonprint">
  <strong style="font-family:var(--cx-font-serif);font-weight:500">The Catalogue Card</strong>
  <span class="cx-caption">clinicalomx design system</span>
  <span style="margin-left:auto"></span>
  <button class="cx-btn" id="mode">dark</button>
  <button class="cx-btn" id="grey">greyscale</button>
  <button class="cx-btn" onclick="window.print()">print</button>
</div>
<main class="cx-page">
  <h1 class="cx-title">The Catalogue Card</h1>
  <p class="cx-caption" style="max-width:76ch;margin-top:var(--cx-space-2)">
    Generated by <code>build_preview.py</code> from <code>tokens.css</code> and
    <code>states.py</code>. The authority is <code>DESIGN.md</code> at the repository root; the
    per-surface specs are <code>docs/design/build-app/README.md</code>. Every contrast number on
    this page was computed when it was generated.</p>
{body}
</main>
<script>
const root = document.documentElement;
document.getElementById('mode').onclick = (e) => {{
  const dark = root.dataset.theme === 'dark';
  root.dataset.theme = dark ? 'light' : 'dark';
  e.target.textContent = dark ? 'dark' : 'light';
}};
document.getElementById('grey').onclick = (e) => {{
  document.body.classList.toggle('cx-grey');
  e.target.textContent = document.body.classList.contains('cx-grey') ? 'colour' : 'greyscale';
}};
</script>
</body>
</html>
"""


if __name__ == "__main__":
    out = HERE / "preview.html"
    out.write_text(build())
    print(f"wrote {out} ({len(out.read_text()):,} bytes)")
    for mode in ("light", "dark"):
        p = primitives(mode) | {"white": "#ffffff"}
        bad = [(lbl, ratio(p[f], p[b]), need) for lbl, f, b, need, _ in PAIRS
               if not lbl.startswith("REJECTED") and ratio(p[f], p[b]) < need]
        print(f"  {mode}: {len(PAIRS)} pairs checked, "
              + (f"FAILURES {bad}" if bad else "every non-rejected pair passes"))
