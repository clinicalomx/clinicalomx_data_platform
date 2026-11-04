"""Features, from ``data/features.csv``, the declared set. ``audit`` reports any others.

``status`` is ``active`` or ``deprecated``. Deprecated rows stay in the file but are
not seeded.
"""

from __future__ import annotations

from ..dataio import features, resolve_dtype

NAME = "features"


def apply(rec, ctx: dict) -> None:
    type_records = ctx.get("types", {})
    out: dict = {}
    for row in features():
        if (row.get("status") or "active").strip() == "deprecated":
            continue
        name = row["name"].strip()
        dtype = resolve_dtype(row["dtype"], type_records)
        kwargs = {}
        if row.get("description"):
            kwargs["description"] = row["description"]
        if (row.get("coerce") or "").strip().lower() in {"1", "true", "yes"}:
            kwargs["coerce"] = True
        out[name] = rec.feature(name, dtype, **kwargs)
    ctx["features"] = out
