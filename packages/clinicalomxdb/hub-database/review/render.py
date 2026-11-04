"""The review page: one self-contained HTML file, no scripts and no external assets.

Every diff line and sampled row is an element with its own id, so it can be annotated. The page
writes nothing; fix the declaration or seed row and re-apply instead.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

import compare
from compare import DTYPE_DRIFT, EXTRA, MATCH, MISSING, RETIRED, UNRESOLVED

_VERDICT_TEXT = {
    MATCH: "landed",
    MISSING: "DID NOT LAND",
    DTYPE_DRIFT: "DTYPE DISAGREES",
    UNRESOLVED: "unresolved",
    RETIRED: "deprecated, absent",
    EXTRA: "undeclared",
}

_STYLE = """
:root {
  --ink: #16191d; --dim: #5b6470; --line: #d8dde4; --bg: #ffffff; --panel: #f7f8fa;
  --bad-bg: #fdecec; --bad-ink: #8c1c1c; --bad-line: #e5a3a3;
  --warn-bg: #fdf6e3; --warn-ink: #7a5a10;
  --ok-ink: #1d6b3f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e6e9ee; --dim: #99a3b0; --line: #333a44; --bg: #14171b; --panel: #1b1f25;
    --bad-bg: #3a1d1d; --bad-ink: #ffb3b3; --bad-line: #7a3838;
    --warn-bg: #33290f; --warn-ink: #f0d79a;
    --ok-ink: #7fd6a3;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem 6rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .35rem; }
h3 { font-size: .95rem; margin: 1.5rem 0 .35rem; color: var(--dim);
     text-transform: uppercase; letter-spacing: .06em; }
p { margin: .4rem 0; }
.sub, .note { color: var(--dim); font-size: .875rem; }
.meta { margin: .75rem 0 0; padding: .75rem 1rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 6px; font-size: .875rem; }
.meta div { margin: .1rem 0; }
.banner { margin: .5rem 0 0; padding: .6rem .9rem; border-radius: 6px; font-weight: 600; }
.banner.pass { color: var(--ok-ink); border: 1px solid var(--line); background: var(--panel); }
.banner.fail { color: var(--bad-ink); border: 1px solid var(--bad-line); background: var(--bad-bg); }
.banner.warn { color: var(--warn-ink); border: 1px solid var(--warn-ink); background: var(--warn-bg); }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; margin: .6rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { background: var(--panel); font-weight: 600; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
              font-size: .95em; }
tr.bad { background: var(--bad-bg); }
tr.bad td { color: var(--bad-ink); }
tr.warn { background: var(--warn-bg); }
tr.warn td { color: var(--warn-ink); }
td.verdict { white-space: nowrap; font-weight: 600; }
/* A refused cell and one nobody judged sit in differently coloured rows, and the cell itself is
   marked too, so the reader can tell which value the verdict is about. */
td.flagged { font-weight: 700; text-decoration: underline; }
td.unjudged { font-style: italic; }
.empty { padding: .75rem 1rem; color: var(--dim); font-size: .875rem; }
.verdict-box { margin-top: .75rem; padding: 1rem 1.15rem; border: 1px solid var(--line);
               border-radius: 6px; background: var(--panel); }
.verdict-box ol { margin: .5rem 0 0; padding-left: 1.2rem; }
.verdict-box li { margin: .3rem 0; }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _endpoint(landed: dict) -> str:
    """Which database this run read, from the four connection fields. Never prints a password."""
    connection = landed.get("connection") or {}
    host = connection.get("host") or ""
    port = connection.get("port") or ""
    database = connection.get("database") or ""
    role = connection.get("role") or ""
    if not host and not database:
        return "not recorded"
    if not host:
        return database
    where = f"{host}:{port}" if port else host
    return f"{role or 'unknown role'}@{where}/{database}"


def _row_class(verdict: str) -> str:
    if verdict in compare.FAILURES:
        return "bad"
    if verdict == UNRESOLVED:
        return "warn"
    return ""


def _schema_phrases(gaps: dict) -> list[str]:
    """Why panel 1 confirmed less than the whole declaration."""
    phrases = []
    if gaps["unresolved"]:
        phrases.append(f"{len(gaps['unresolved'])} could not be confirmed either way")
    if gaps["retired"]:
        count = len(gaps["retired"])
        phrases.append(f"{count} deprecated name{'' if count == 1 else 's'} did not land, which "
                       "is what deprecated means")
    return phrases


def _gap_phrases(gaps: dict) -> list[str]:
    """How each gap is worded."""
    phrases = []
    if gaps["skipped"]:
        phrases.append(f"{len(gaps['skipped'])} were left unjudged")
    if gaps["absent_columns"]:
        count = len(gaps["absent_columns"])
        phrases.append(f"{count} merged feature column{'' if count == 1 else 's'} never reached "
                       "the frame")
    if gaps["unjudged_registries"]:
        count = len(gaps["unjudged_registries"])
        phrases.append(f"{count} sampled registr{'y' if count == 1 else 'ies'} had no cell judged "
                       "at all")
    if gaps["dropped_registries"]:
        count = len(gaps["dropped_registries"])
        phrases.append(f"{count} named registr{'y was' if count == 1 else 'ies were'} not sampled "
                       "at all")
    return phrases


def _serial(phrases: list[str]) -> str:
    if len(phrases) < 3:
        return " and ".join(phrases)
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]


def _called_out(rows: list[dict]) -> list[dict]:
    """Rows named above the tables: failures, then unconfirmed or retired rows, once each."""
    named = compare.failures(rows)
    seen = {id(row) for row in named}
    for group in (compare.unresolved(rows), compare.retired(rows)):
        named += [row for row in group if id(row) not in seen]
        seen |= {id(row) for row in group}
    return named


def _table(headers: list[str], body: str, empty: str) -> str:
    if not body:
        return f'<div class="scroll"><p class="empty">{_e(empty)}</p></div>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _features_table(rows: list[dict]) -> str:
    body = ""
    for row in rows:
        verdict = row["verdict"]
        expected = row["expected_dtype"] or row["declared_dtype"]
        body += (
            f'<tr id="feature-{_e(row["name"])}" class="{_row_class(verdict)}">'
            f'<td class="mono">{_e(row["name"])}</td>'
            f'<td class="mono">{_e(row["declared_dtype"])}</td>'
            f'<td class="mono">{_e(expected)}</td>'
            f'<td class="mono">{_e(row["landed_dtype"])}</td>'
            f'<td>{_e(row["status"])}</td>'
            f'<td class="verdict">{_e(_VERDICT_TEXT[verdict])}</td></tr>')
    return _table(["feature", "declared dtype", "expected in the instance", "landed dtype",
                   "status", "verdict"], body, "nothing declared and nothing landed")


def _types_table(rows: list[dict]) -> str:
    body = ""
    for row in rows:
        verdict = row["verdict"]
        body += (
            f'<tr id="type-{_e(row["name"])}" class="{_row_class(verdict)}">'
            f'<td class="mono">{_e(row["name"])}</td>'
            f'<td class="mono">{_e(row["parent"])}</td>'
            f'<td class="mono">{_e(row["declared_uid"])}</td>'
            f'<td class="mono">{_e(row["landed_uid"])}</td>'
            f'<td>{_e(row["count"])}</td>'
            f'<td class="verdict">{_e(_VERDICT_TEXT[verdict])}</td></tr>')
    return _table(["entity type", "parent", "declared uid", "landed uid", "records", "verdict"],
                  body, "no entity types declared or landed")


def _storages_table(rows: list[dict]) -> str:
    body = ""
    for index, row in enumerate(rows):
        verdict = row["verdict"]
        body += (
            f'<tr id="storage-{index}" class="{_row_class(verdict)}">'
            f'<td class="mono">{_e(row["label"])}</td>'
            f'<td class="mono">{_e(row["root"])}</td>'
            f'<td class="mono">{_e(row["landed_uid"])}</td>'
            f'<td class="verdict">{_e(_VERDICT_TEXT[verdict])}</td></tr>')
    return _table(["declared key or landed description", "root", "landed uid", "verdict"], body,
                  "no storage roots declared or landed")


def _sample_table(sample: dict, problems: list[dict], skipped: list[dict]) -> str:
    registry = sample.get("registry", "")
    columns = sample.get("columns") or []
    merged = set(sample.get("merged") or [])
    flagged: dict[int, list[dict]] = {}
    for problem in problems:
        flagged.setdefault(problem["row"], []).append(problem)
    unjudged: dict[int, list[dict]] = {}
    for skip in skipped:
        unjudged.setdefault(skip["row"], []).append(skip)

    # Panel 2 judges only the merged feature columns.
    headers = [f"{c} (feature)" if c in merged else c for c in columns]
    body = ""
    for position, row in enumerate(sample.get("rows") or []):
        hits = flagged.get(position, [])
        holds = unjudged.get(position, [])
        bad_columns = {problem["column"] for problem in hits}
        warn_columns = {skip["column"] for skip in holds}
        cells = ""
        for index, column in enumerate(columns):
            value = row[index]
            # An encoded image cell shows its length.
            if len(value) > 200:
                value = f"{value[:120]} ... ({len(value)} characters)"
            mark = ("flagged" if column in bad_columns
                    else "unjudged" if column in warn_columns else "")
            cells += f'<td class="mono {mark}">{_e(value)}</td>'
        row_class = "bad" if hits else "warn" if holds else ""
        body += f'<tr id="row-{_e(registry)}-{position}" class="{row_class}">{cells}</tr>'
        for problem in hits:
            body += (f'<tr id="flag-{_e(registry)}-{position}-{_e(problem["column"])}" '
                     f'class="bad"><td colspan="{len(columns)}">'
                     f'{_e(problem["column"])}: {_e(problem["problem"])}</td></tr>')
        for skip in holds:
            body += (f'<tr id="skip-{_e(registry)}-{position}-{_e(skip["column"])}" '
                     f'class="warn"><td colspan="{len(columns)}">'
                     f'{_e(skip["column"])}: not judged, {_e(skip["reason"])}</td></tr>')
    return _table(headers, body, f"{registry} returned no rows")


def render(context: dict) -> str:
    """The whole page. Nothing is computed here beyond counting."""
    declared = context["declared"]
    landed = context["landed"]
    sections = context["schema"]
    samples = context["samples"]

    all_rows = compare.schema_rows(sections)
    # Every count comes from this one report.
    gaps = compare.gap_report(all_rows, samples, context.get("validated") or [],
                              context.get("dropped_registries") or [])
    problems = gaps["problems"]
    skipped = gaps["skipped"]
    absent_columns = gaps["absent_columns"]
    judged = gaps["judged"]
    schema_failures = compare.failures(all_rows)
    schema_unresolved = gaps["unresolved"]
    # Failing and unconfirmed names, grouped by verdict, from the rows the banner counts.
    callouts: dict[str, list[str]] = {}
    for label, rows, key in (("feature", sections["features"], "name"),
                             ("entity type", sections["types"], "name"),
                             ("storage root", sections["storages"], "root")):
        for row in _called_out(rows):
            callouts.setdefault(row["verdict"], []).append(f"{label} {row[key]}")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>clinicalOMx schema review</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        "<h1>Schema review</h1>",
        '<p class="sub">Read-only. A correction is an annotation here, then a change to the '
        "declaration, a seed row or a curation rule, then a re-apply. Never a hand-edit of the "
        "instance.</p>",
        '<div class="meta">',
        f'<div>instance: <span class="mono">{_e(landed.get("instance") or "not connected")}</span></div>',
        f'<div>database: <span class="mono">{_e(_endpoint(landed))}</span></div>',
        f'<div>declaration: <span class="mono">{_e(declared.get("source"))}</span>'
        f'{" version " + _e(declared["version"]) if declared.get("version") else ""}</div>',
        # The declared side's size, so an empty comparison reads as empty.
        f'<div>declared: {len(declared.get("features") or [])} features, '
        f'{len(declared.get("types") or [])} entity types, '
        f'{len(declared.get("storages") or [])} storage roots</div>',
        f"<div>generated: {_e(generated)}</div>",
        "</div>",
    ]

    # Panel 1. Green means every declared name was confirmed. Unresolved rows never fail it.
    schema_clause = _serial(_schema_phrases(gaps))
    if schema_failures:
        banner = ("fail", f"{len(schema_failures)} declared names did not land as declared"
                          + (f", and {schema_clause}" if schema_clause else ""))
    elif schema_clause:
        banner = ("warn", f"no declared name failed, and {schema_clause}")
    else:
        banner = ("pass", "every declared name was confirmed to land as declared")
    parts += [
        "<h2>1. Declared versus landed</h2>",
        '<p class="note">A declared name that did not land breaks the floor and is a failure. '
        "A landed name nobody declared is expected in a lakehouse and never fails "
        "(ADR 0003 Decision 2). A declared dtype naming something the landed side cannot vouch "
        "for is unresolved: a question rather than a verdict, because asserting drift the "
        "reviewer cannot check would be a fabricated failure.</p>",
        f'<div class="banner {banner[0]}">{_e(banner[1])}</div>',
    ]
    for verdict, names in callouts.items():
        parts.append(
            f'<p class="note">{_e(_VERDICT_TEXT[verdict])} ({len(names)}): '
            f'<span class="mono">{_e(", ".join(names))}</span></p>')
    parts += [
        "<h3>Features</h3>", _features_table(sections["features"]),
        "<h3>Entity types</h3>", _types_table(sections["types"]),
        "<h3>Storage roots</h3>", _storages_table(sections["storages"]),
    ]

    # Panel 2. Green means judged and clean. Unjudged cells and absent columns are counted, not
    # flagged.
    gap_clause = _serial(_gap_phrases(gaps))
    if problems:
        banner = ("fail", f"{len(problems)} sampled values were refused by a validator"
                          + (f", and {gap_clause}" if gap_clause else ""))
    elif not judged:
        banner = ("warn", "no sampled value was judged, so nothing on this panel was checked")
    elif gap_clause:
        banner = ("warn", f"{judged} sampled values were judged and none was refused, and "
                          f"{gap_clause}")
    else:
        banner = ("pass", f"all {judged} sampled values were judged and none was refused")
    parts += [
        "<h2>2. Sampled rows, validated</h2>",
        '<p class="note">Feature columns only. A model column is typed by the database; a '
        "feature value is a row in a value table and can be anything. The checks are "
        "lamin_api's own, so a flag here is the query surface refusing the value. A cell this "
        "reviewer cannot decompose or has no vocabulary for is counted as unjudged rather than "
        "flagged, because reporting a correct row as wrong would cost more than the missed "
        "coverage.</p>",
        f'<div class="banner {banner[0]}">{_e(banner[1])}</div>',
    ]
    for reason, count in gaps["reasons"].items():
        parts.append(f'<p class="note">unjudged ({count}): {_e(reason)}</p>')
    for entry in absent_columns:
        parts.append(
            f'<p class="note">{_e(entry["registry"])}: the feature '
            f'<span class="mono">{_e(entry["column"])}</span> reached no column in the frame, so '
            "none of its values was judged. A frame carrying no uid merges no feature at all.</p>")
    if gaps["dropped_registries"]:
        parts.append(
            '<p class="note">Sampling is capped because each registry costs several queries '
            "against an instance tuned for connection scarcity, so these named registries were "
            "not sampled at all: "
            f'<span class="mono">{_e(", ".join(gaps["dropped_registries"]))}</span>. '
            "Nothing on this page says anything about their rows.</p>")
    if not samples:
        parts.append('<p class="empty">No registry was sampled.</p>')
    for sample, entry in zip(samples, gaps["registries"]):
        # Every gap is named in the heading. The judged count leads.
        judged_here = (f'{entry["judged"]} cells judged' if entry["judged"]
                       else "NO cell judged")
        heading = (f'{entry["rows"]} rows, {judged_here}, {len(entry["problems"])} flagged, '
                   f'{len(entry["skipped"])} unjudged')
        if entry["absent"]:
            heading += f', {len(entry["absent"])} merged features reached no column'
        parts.append(f'<h3>{_e(entry["registry"])} <span class="mono">{heading}</span></h3>')
        parts.append(_sample_table(sample, entry["problems"], entry["skipped"]))

    # Panel 3 carries every gap the panels above do.
    parts += [
        "<h2>3. Verdict</h2>",
        '<div class="verdict-box">',
        f"<p><strong>{len(schema_failures)}</strong> schema failures, "
        f"<strong>{len(problems)}</strong> flagged values, over "
        f"<strong>{len(samples)}</strong> sampled registries.</p>",
        f'<p class="note">The reviewer declined to judge '
        f"<strong>{len(schema_unresolved)}</strong> declared dtypes and "
        f"<strong>{len(skipped)}</strong> sampled cells, out of "
        f'<strong>{gaps["looked_at"]}</strong> cells it looked at, and '
        f"<strong>{len(absent_columns)}</strong> merged feature columns never reached a frame at "
        "all. Those are questions, not verdicts, and they are what a green panel above does not "
        "cover.</p>",
    ]
    if gaps["retired"]:
        names = ", ".join(row.get("name") or row.get("root", "") for row in gaps["retired"])
        parts.append(
            f'<p class="note"><strong>{len(gaps["retired"])}</strong> declared names did not land '
            "because they are deprecated, which is the declaration working rather than the floor "
            f'breaking: <span class="mono">{_e(names)}</span>. Nothing above claims they landed, '
            "and a name you did not expect to see here is a declaration still to retire.</p>")
    if gaps["unjudged_registries"]:
        parts.append(
            f'<p class="note">No sampled cell was judged in '
            f'<span class="mono">{_e(", ".join(gaps["unjudged_registries"]))}</span>, so nothing '
            "on this page says those rows are clean.</p>")
    if gaps["dropped_registries"]:
        parts.append(
            f'<p class="note">The sampling cap left '
            f'<span class="mono">{_e(", ".join(gaps["dropped_registries"]))}</span> unsampled '
            "although it was named, so the count above covers fewer registries than were asked "
            "for.</p>")
    parts += [
        "<p>Annotate the row or diff line above that is wrong, then:</p>",
        "<ol>"
        "<li>a name that did not land is a change to the declaration or to the apply step that "
        "seeds it</li>"
        "<li>a dtype that disagrees is a change to the declaration, and a backfill if records "
        "already carry values under the old dtype</li>"
        "<li>a flagged value is a change to the curation rule that wrote it</li>"
        "<li>then re-apply and regenerate this page</li>"
        "</ol>",
        "<p class=\"note\">Nothing on this page writes to the instance, so a fix applied by hand "
        "would leave the declaration that produced the defect still wrong.</p>",
        "</div>",
        "</main></body></html>",
    ]
    return "".join(parts)
