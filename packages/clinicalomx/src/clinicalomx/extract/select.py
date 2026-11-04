"""Finding the ROI somebody meant.

Tier 1 predicates are pushed into the :class:`~clinicalomx.extract.source.Source` backend; tier 2
predicates scan the in-memory index. The free-text resolver is deterministic and returns
ambiguous, with no pick, unless exactly one candidate has the full score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from clinicalomx.extract.request import RoiQuery
from clinicalomx.extract.source import Selection, default_source
from clinicalomx.names import FEATURES
from clinicalomx.snapshot import Snapshot

#: `<slide>:TMA<t>:R<r>C<c>` pieces.
_TMA = re.compile(r"\btma[\s_-]*(\d+)\b", re.I)
_CELL = re.compile(r"\br[\s_-]*(\d+)[\s_-]*c[\s_-]*(\d+)\b", re.I)
#: Words that carry no slide identity, including format nouns such as "ome.tiff".
_NOISE = frozenset({
    "the", "a", "an", "of", "in", "on", "for", "from", "give", "me", "get", "core", "cores",
    "tma", "slide", "whole", "wholeslide", "roi", "please", "and", "as", "to", "it", "at",
    "ome.tiff", "ometiff", "ome", "tiff", "tif", "spatialdata", "sdata", "anndata", "h5ad",
    "zarr", "spatialexperiment", "format", "image", "table", "belonging", "its", "with",
})


def narrow(*, roi_type: str | None = None, name_contains: str | None = None,
           has_roles=(), state: str | None = None, parent_uid: str | None = None,
           limit: int = 500, source=None) -> list:
    """The tier-1 ROI query, as a :class:`~clinicalomx.extract.source.Selection`."""
    src = source if source is not None else default_source()
    return src.entities(Selection(
        roi_type=roi_type, name_contains=name_contains, has_roles=tuple(has_roles),
        state=state, parent_uid=parent_uid, limit=limit))


@dataclass(frozen=True)
class Candidate:
    uid: str
    name: str
    roi_type: str
    score: float
    why: str

    def to_dict(self) -> dict:
        return {"uid": self.uid, "name": self.name, "roi_type": self.roi_type,
                "score": round(self.score, 3), "why": self.why}


@dataclass(frozen=True)
class Resolution:
    query: str
    parsed: dict[str, str]
    exact: Candidate | None
    candidates: tuple[Candidate, ...]
    ambiguous: bool
    #: How many candidates tie for the best score.
    tied: int = 0

    def to_dict(self) -> dict:
        return {"query": self.query, "parsed": dict(self.parsed),
                "exact": self.exact.to_dict() if self.exact else None,
                "candidates": [c.to_dict() for c in self.candidates],
                "ambiguous": self.ambiguous, "tied": self.tied}

    def caveats(self) -> list[str]:
        if self.ambiguous:
            return [f"AMBIGUOUS: {self.tied} ROIs match {self.query!r} equally well. Ask which "
                    f"one was meant; do not choose."]
        if self.exact is None:
            return [f"No ROI matches {self.query!r}. Do not infer one from the naming convention."]
        return []


def parse(query: str) -> dict[str, str]:
    """Pull the TMA index, the R/C cell and the slide tokens out of free text."""
    out: dict[str, str] = {}
    if (m := _TMA.search(query)):
        out["tma"] = f"TMA{int(m.group(1))}"
    if (m := _CELL.search(query)):
        out["cell"] = f"R{int(m.group(1))}C{int(m.group(2))}"
    rest = _CELL.sub(" ", _TMA.sub(" ", query))
    tokens = [t for t in re.split(r"[^\w.-]+", rest.lower()) if t and t not in _NOISE]
    if tokens:
        out["slide_tokens"] = " ".join(tokens)
    return out


def _score(name: str, parsed: dict[str, str]) -> tuple[float, list[str]]:
    """How well one Record name answers the parsed query, and why."""
    segments = name.split(":")
    lowered = name.lower()
    points, total, why = 0.0, 0.0, []

    if "cell" in parsed:
        total += 1
        if segments[-1].upper() == parsed["cell"]:
            points += 1
            why.append(f"cell {parsed['cell']} exact")
    if "tma" in parsed:
        total += 1
        if any(s.upper() == parsed["tma"] for s in segments):
            points += 1
            why.append(f"{parsed['tma']} exact")
    if "slide_tokens" in parsed:
        tokens = parsed["slide_tokens"].split()
        total += 1
        hit = sum(1 for t in tokens if t in lowered)
        if hit:
            points += hit / len(tokens)
            why.append(f"{hit}/{len(tokens)} slide token(s) matched")
    return (points / total if total else 0.0), why


def resolve(query: str, records, *, roi_type: str | None = None, limit: int = 10) -> Resolution:
    """Match free text against ROI Record names. Never disambiguates.

    Exactly one full-score candidate gives ``exact``; anything else is ``ambiguous`` with
    ``exact=None``.
    """
    parsed = parse(query)
    scored: list[Candidate] = []
    for rec in records:
        rtype = getattr(rec, "type", None)
        rtype = str(getattr(rtype, "name", rtype)) if rtype is not None else ""
        if roi_type and rtype != roi_type:
            continue
        if str(rec.name) == query.strip():
            scored.append(Candidate(rec.uid, str(rec.name), rtype, 1.0, "exact name match"))
            continue
        score, why = _score(str(rec.name), parsed)
        if score > 0:
            scored.append(Candidate(rec.uid, str(rec.name), rtype, score, "; ".join(why)))

    scored.sort(key=lambda c: (-c.score, c.name))
    best = scored[0].score if scored else 0.0
    top = [c for c in scored if c.score == best] if scored else []
    exact = top[0] if len(top) == 1 and best >= 1.0 else None
    return Resolution(
        query=query, parsed=parsed, exact=exact,
        candidates=tuple(scored[:limit]),
        ambiguous=exact is None and len(scored) > 0,
        tied=len(top),
    )


def find(query: str, *, roi_type: str | None = None, limit: int = 10, source=None) -> Resolution:
    """:func:`resolve` against the source, narrowed in tier 1 first where the text allows it."""
    parsed = parse(query)
    hint = parsed.get("cell") or parsed.get("tma")
    records = narrow(roi_type=roi_type, name_contains=hint, limit=5_000, source=source)
    if not records and hint:
        records = narrow(roi_type=roi_type, limit=5_000, source=source)
    return resolve(query, records, roi_type=roi_type, limit=limit)


def select(selector, *, snapshot=None, source=None) -> list:
    """The ROI Records an :class:`~clinicalomx.extract.request.ROISelector` names.

    An ambiguous ``fuzzy`` selector raises.
    """
    snap = snapshot if snapshot is not None else Snapshot()
    src = source if source is not None else default_source()

    # The Snapshot first, so a pre-populated Snapshot resolves with no instance.
    if selector.uid:
        found = snap.record_by_uid(selector.uid) or src.entity(uid=selector.uid)
        roots = [found] if found is not None else []
    elif selector.name:
        found = snap.record_by_any_name(selector.name) or src.entity(
            name=selector.name, is_type=False)
        roots = [found] if found is not None else []
    elif selector.fuzzy:
        res = find(selector.fuzzy, roi_type=selector.child_type, source=source)
        if res.exact is None:
            names = ", ".join(c.name for c in res.candidates[:5]) or "nothing"
            raise ValueError(
                f"{selector.fuzzy!r} does not resolve to exactly one ROI (matched: {names}). "
                f"Name the one you meant with `uid` or `name`.")
        roots = [snap.record_by_uid(res.exact.uid) or src.entity(uid=res.exact.uid)]
    else:
        q = selector.query
        # Built from `RoiQuery.PUSHDOWN`.
        roots = narrow(limit=q.limit, source=source,
                       **{kw: getattr(q, field) for field, kw in RoiQuery.PUSHDOWN.items()})
        roots = _scan(roots, q, snap)

    if selector.include == "self":
        return roots
    walk = snap.children if selector.include == "children" else snap.descendants
    out = [c for r in roots for c in walk(r, roi_type=selector.child_type)]
    return list({r.uid: r for r in out}.values())


def _scan(records, query, snap) -> list:
    """Tier 2: the predicates SQL cannot express safely, over the bulk-loaded values."""
    if not any(getattr(query, field) for field in RoiQuery.SCANNED):
        return records
    out = []
    for rec in records:
        values = snap.values(rec)
        if query.cohort and str(values.get(FEATURES.COHORT, "")) != query.cohort:
            continue
        if query.patient_id and str(values.get(FEATURES.PATIENT_ID, "")) != query.patient_id:
            continue
        if query.slide and str(rec.name).split(":")[0] != query.slide:
            continue
        out.append(rec)
    return out
