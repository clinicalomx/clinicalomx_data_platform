"""Declared versus landed schema, and lamin_api's validators run over sampled rows.

Pure functions: no lamindb import, no database, no file reads. A declared name that did not
land is a failure. A landed name nobody declared is reported without failing.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# Verdicts on one declared or landed name. Only FAILURES turn a panel red.
MATCH = "ok"
MISSING = "missing"          # declared and did not land
DTYPE_DRIFT = "dtype"        # landed under a different dtype than declared
UNRESOLVED = "unresolved"    # declared dtype names something the landed side cannot vouch for
RETIRED = "retired"          # declared deprecated and absent, which is what deprecated means
EXTRA = "extra"              # landed and undeclared

FAILURES = (MISSING, DTYPE_DRIFT)

# A declaration writes a categorical as type:<Name>; lamindb stores cat[<Registry>[<uid>]].
_TYPE_REF = re.compile(r"^type:(.+)$")
# A bare registry name, optionally module-qualified as in bionty.Protein, means cat[<Registry>].
_BARE_REGISTRY = re.compile(r"^([a-z][A-Za-z0-9_]*\.)?[A-Z][A-Za-z0-9_]*$")


def registry_spellings(landed: list[dict]) -> set[str]:
    """Every spelling of a landed registry a declaration may use: bionty qualified, lamindb bare."""
    spellings = set()
    for row in landed:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        module = (row.get("module") or "").strip()
        spellings.add(f"{module}.{name}" if module and module != "lamindb" else name)
    return spellings


def normalise_declared_dtype(spec: str, type_uids: dict[str, str],
                             registries: set[str]) -> tuple[str, bool]:
    """A declared dtype in the spelling lamindb stores, and whether it could be resolved.

    type_uids maps a declared type name to its landed uid; registries confirms bare registry
    names. A name neither confirms is unresolved, not drift.
    """
    spec = (spec or "").strip()
    # A blank dtype cell is no claim, so it is unresolved, not a match.
    if not spec:
        return spec, False
    ref = _TYPE_REF.match(spec)
    if ref:
        name = ref.group(1).strip()
        uid = type_uids.get(name)
        return (f"cat[Record[{uid}]]", True) if uid else (spec, False)
    if _BARE_REGISTRY.match(spec):
        # Unconfirmed bare tokens are unresolved, as in the type: branch.
        return (f"cat[{spec}]", True) if spec in registries else (spec, False)
    return spec, True


def diff_features(declared: list[dict], landed: list[dict], type_uids: dict[str, str],
                  registries: set[str]) -> list[dict]:
    """One row per feature name, from either side, with a verdict."""
    by_name = {f["name"]: f for f in landed}
    rows = []
    seen = set()

    for spec in declared:
        name = spec["name"]
        seen.add(name)
        expected, resolved = normalise_declared_dtype(spec["dtype"], type_uids, registries)
        live = by_name.get(name)
        if live is None:
            # A deprecated name is declared but never seeded.
            verdict = RETIRED if spec["status"] == "deprecated" else MISSING
            rows.append(_feature_row(name, spec, expected, "", verdict))
            continue
        actual = (live.get("dtype") or "").strip()
        if not resolved:
            verdict = UNRESOLVED
        elif actual == expected:
            verdict = MATCH
        else:
            verdict = DTYPE_DRIFT
        rows.append(_feature_row(name, spec, expected, actual, verdict))

    for live in landed:
        if live["name"] in seen:
            continue
        rows.append({
            "name": live["name"], "declared_dtype": "", "expected_dtype": "",
            "landed_dtype": (live.get("dtype") or "").strip(),
            "status": "", "description": live.get("description") or "", "verdict": EXTRA,
        })
    return rows


def _feature_row(name, spec, expected, actual, verdict) -> dict:
    return {
        "name": name,
        "declared_dtype": spec["dtype"],
        "expected_dtype": expected,
        "landed_dtype": actual,
        "status": spec["status"],
        "description": spec.get("description") or "",
        "verdict": verdict,
    }


def diff_types(declared: list[dict], landed: list[dict]) -> list[dict]:
    """Entity type records, by name. A changed uid is drift: the type was deleted and recreated."""
    by_name = {t["name"]: t for t in landed}
    rows = []
    seen = set()
    for spec in declared:
        name = spec["name"]
        seen.add(name)
        live = by_name.get(name)
        if live is None:
            rows.append({"name": name, "declared_uid": spec["uid"], "landed_uid": "",
                         "parent": spec["parent"], "count": "", "verdict": MISSING})
            continue
        landed_uid = (live.get("uid") or "").strip()
        drifted = bool(spec["uid"]) and bool(landed_uid) and spec["uid"] != landed_uid
        rows.append({"name": name, "declared_uid": spec["uid"], "landed_uid": landed_uid,
                     "parent": spec["parent"], "count": live.get("count", ""),
                     "verdict": DTYPE_DRIFT if drifted else MATCH})
    for live in landed:
        if live["name"] in seen:
            continue
        rows.append({"name": live["name"], "declared_uid": "",
                     "landed_uid": (live.get("uid") or "").strip(), "parent": "",
                     "count": live.get("count", ""), "verdict": EXTRA})
    return rows


def diff_storages(declared: list[dict], landed: list[dict]) -> list[dict]:
    """Storage roots, matched on root path rather than key."""
    by_root = {s["root"]: s for s in landed if s.get("root")}
    rows = []
    seen = set()
    for spec in declared:
        root = spec["root"]
        seen.add(root)
        live = by_root.get(root)
        # The declared key on a declared row, the landed description otherwise.
        rows.append({"label": spec["key"], "root": root,
                     "landed_uid": (live or {}).get("uid", ""),
                     "verdict": MATCH if live else MISSING})
    for live in landed:
        if live.get("root") in seen:
            continue
        rows.append({"label": live.get("description", ""), "root": live.get("root", ""),
                     "landed_uid": live.get("uid", ""), "verdict": EXTRA})
    return rows


def schema_rows(schema: dict) -> list[dict]:
    """Every diff row the three sections produced, as one list."""
    return schema["features"] + schema["types"] + schema["storages"]


def failures(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["verdict"] in FAILURES]


def unresolved(rows: list[dict]) -> list[dict]:
    """Rows the reviewer declined to judge. Neither failures nor confirmations."""
    return [r for r in rows if r["verdict"] == UNRESOLVED]


def retired(rows: list[dict]) -> list[dict]:
    """Declared deprecated names that did not land. Neither failures nor confirmations."""
    return [r for r in rows if r["verdict"] == RETIRED]


def gap_report(rows: list[dict], samples: list[dict], validated: list[dict],
               dropped_registries: list[str] | None = None) -> dict:
    """Everything this run did not look at, and everything it did judge, counted once.

    Every surface that makes a claim about the run reads this: both banners, the per-registry
    headings, the verdict box and the stderr line.

    validated is validate_sample's result per sample, aligned by position with samples.
    dropped_registries names requested registries that collect's cap did not sample.
    """
    registries = []
    problems: list[dict] = []
    skipped: list[dict] = []
    absent_columns: list[dict] = []
    judged = 0
    for sample, result in zip(samples, validated):
        name = sample.get("registry", "")
        absent = [{"registry": name, "column": column} for column in result["absent_columns"]]
        registries.append({
            "registry": name,
            "rows": len(sample.get("rows") or []),
            "judged": result["judged"],
            "problems": result["problems"],
            "skipped": result["skipped"],
            "absent": absent,
        })
        problems += result["problems"]
        skipped += result["skipped"]
        absent_columns += absent
        judged += result["judged"]

    reasons: dict[str, int] = {}
    for skip in skipped:
        reasons[skip["reason"]] = reasons.get(skip["reason"], 0) + 1

    return {
        "unresolved": unresolved(rows),
        "retired": retired(rows),
        "problems": problems,
        "skipped": skipped,
        "reasons": reasons,
        "absent_columns": absent_columns,
        "judged": judged,
        "looked_at": judged + len(skipped),
        "registries": registries,
        "dropped_registries": list(dropped_registries or []),
        # A registry whose sampled rows carry no feature value judges nothing and records no gap.
        "unjudged_registries": [r["registry"] for r in registries if not r["judged"]],
    }


# --- row validation ---------------------------------------------------------------------
# kind and scalar are lamin_api's own functions, passed in. Never copy them here.

# merge_feature_columns joins a multi-valued cell with this. A permitted value can contain it,
# so a joined cell matches whole or goes unjudged.
_JOINED = ", "

# Why one cell went unjudged. None is a defect in the row.
NO_PERMITTED_VALUES = "the feature carries no permitted value to check against"
PERMITTED_VALUES_INCOMPLETE = ("lamin_api read only part of the permitted-value list, so a value "
                               "it did not read would look invalid")
MULTI_VALUED = ("several values joined with the separator, which a permitted value can itself "
                "contain")


def validate_sample(sample: dict, specs: dict[str, dict], kind: Callable[[str], str],
                    scalar: Callable[[str, dict], Any]) -> dict:
    """What one registry's sampled rows were found to be, cell by cell.

    problems turn the panel red. skipped lists cells declined for judgement, judged counts the
    cells checked, and absent_columns names merged features that reached no column. Only merged
    feature columns are checked. An empty cell is neither judged nor skipped.
    """
    problems: list[dict] = []
    skipped: list[dict] = []
    judged = 0
    columns = sample.get("columns") or []
    requested = list(sample.get("merged") or [])
    merged = [c for c in requested if c in columns]
    # lamin_api returns no column for a merged feature when the frame carries no uid.
    absent = [c for c in requested if c not in columns]
    index = {c: i for i, c in enumerate(columns)}

    for position, row in enumerate(sample.get("rows") or []):
        for name in merged:
            raw = (row[index[name]] or "").strip()
            if not raw:
                continue
            spec = specs.get(name)
            if spec is None:
                judged += 1
                problems.append({**_cell(position, row, sample, name, raw),
                                 "problem": "no Feature record defines this column"})
                continue
            problem, reason = _cell_verdict(spec, raw, kind, scalar)
            if reason:
                skipped.append({**_cell(position, row, sample, name, raw), "reason": reason})
                continue
            judged += 1
            if problem:
                problems.append({**_cell(position, row, sample, name, raw), "problem": problem})

    return {"problems": problems, "skipped": skipped, "judged": judged,
            "absent_columns": absent}


def _cell_verdict(spec: dict, raw: str, kind, scalar) -> tuple[str | None, str | None]:
    """One cell's problem, or the reason it was left unjudged. Never both."""
    dtype = spec.get("dtype") or ""
    shape = kind(dtype)

    if shape == "cat":
        permitted = spec.get("values")
        if not permitted:
            return None, NO_PERMITTED_VALUES
        # A value on the list is confirmed even when the list is incomplete.
        if raw in set(permitted):
            return None, None
        # A value off the list is judged only against a complete list. Length cannot tell, because
        # deduplication pulls a truncated list back under the cap.
        if spec.get("values_incomplete"):
            return None, PERMITTED_VALUES_INCOMPLETE
        if _JOINED in raw:
            return None, MULTI_VALUED
        return f"not a permitted value of {dtype}: {raw}", None

    if shape == "num":
        try:
            scalar(raw, {"type": "feature", "dtype": dtype})
        except ValueError:
            if _JOINED in raw:
                return None, MULTI_VALUED
            return f"{dtype} feature holds a value that is not a number", None
    return None, None


def _cell(position: int, row: list, sample: dict, column: str, value: str) -> dict:
    columns = sample.get("columns") or []
    identity = ""
    for key in ("uid", "key", "name"):
        if key in columns:
            identity = row[columns.index(key)]
            if identity:
                break
    return {"registry": sample.get("registry", ""), "row": position, "identity": identity,
            "column": column, "value": value}
