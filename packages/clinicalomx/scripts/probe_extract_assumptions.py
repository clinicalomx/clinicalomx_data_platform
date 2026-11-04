"""Verify three assumptions `clinicalomx.extract` is built on, against the live instance. Read only.

1. **The child index.** Whether a uid join through the `values_record` link table returns the
   same children as the name match `Snapshot._build_graph` uses.
2. **Feature-predicate pushdown.** `filter_with_features` inspects only the first kwarg, so a call
   mixing a field and a feature raises. Confirms one-predicate-per-`filter()` and the failures.
3. **The geometry round-trip.** Which registered geometry parquets still carry `scale_factor`.

Usage, on the control-plane VM or the HPC:

    python scripts/probe_extract_assumptions.py
    python scripts/probe_extract_assumptions.py --tma "<slide>:TMA0"
"""
from __future__ import annotations

import argparse
import traceback

from clinicalomx.names import FEATURES, TYPES

VERDICTS: list[tuple[str, str, str]] = []


def record(probe: str, verdict: str, detail: str = "") -> None:
    VERDICTS.append((probe, verdict, detail))
    print(f"  {verdict:5}  {probe}" + (f"  - {detail}" if detail else ""))


def _first_tma(ln, name: str | None):
    if name:
        rec = ln.Record.filter(name=name, type__name=TYPES.TMA).one_or_none()
        if rec is None:
            raise SystemExit(f"no TMA Record named {name!r}")
        return rec
    rec = ln.Record.filter(type__name=TYPES.TMA).order_by("name").first()
    if rec is None:
        raise SystemExit(f"no {TYPES.TMA} Records on this instance; pass --tma")
    return rec


# Child index
def probe_parent_join(ln, tma) -> None:
    """Does a uid join through `values_record` return the children a name match would?"""
    print(f"\n[1] child index, against {tma.name!r}")

    by_name = [
        r for r in ln.Record.filter(type__name=TYPES.CORE).to_list()
        if str(r.name).rsplit(":", 1)[0] == tma.name
    ]
    print(f"      name spine implies {len(by_name)} Core child(ren)")

    try:
        joined = ln.Record.filter(
            values_record__feature__name=FEATURES.PARENT_ROI,
            values_record__value__uid=tma.uid,
        ).distinct().to_list()
    except Exception as exc:  # noqa: BLE001 - the whole point is to learn which shape fails
        record("uid join via values_record", "NO",
               f"{type(exc).__name__}: {str(exc)[:160]}")
        record("-> use", "NAME", "the name index in Snapshot._build_graph is the only option")
        return

    names_j = {r.name for r in joined}
    names_n = {r.name for r in by_name}
    if names_j == names_n:
        record("uid join via values_record", "YES", f"{len(joined)} child(ren), identical set")
        record("-> use", "UID", "exact; prefer it over the name index")
        return

    record("uid join via values_record", "DIFFERS",
           f"join={len(names_j)} spine={len(names_n)}")
    for label, extra in (("only in join", names_j - names_n), ("only in spine", names_n - names_j)):
        if extra:
            print(f"      {label}: {sorted(extra)[:5]}")
    record("-> use", "INVESTIGATE", "the graph and the names disagree; see roi.parent_matches_*")


# Feature predicate
def _try(label: str, fn) -> object | None:
    try:
        out = fn()
        n = len(out) if out is not None else 0
        record(label, "OK", f"{n} row(s)")
        return out
    except Exception as exc:  # noqa: BLE001 - the error TYPE is the finding
        record(label, "RAISE", f"{type(exc).__name__}: {str(exc)[:140]}")
        return None


def probe_pushdown(ln, role: str | None) -> None:
    """Chained vs mixed field/feature predicates, and what a no-such-label value does."""
    print("\n[2] feature-predicate pushdown (lamindb 2.8.1)")

    if role is None:
        from clinicalomx.snapshot import Snapshot

        roles = Snapshot().role_features()
        if not roles:
            record("role features declared", "NONE",
                   "no installed consumer Vocabulary declares an Artifact-dtype feature")
            return
        role = roles[0]
    print(f"      probing with role feature {role!r}")

    _try("chained: filter(type).filter(role)",
         lambda: ln.Record.filter(type__name=TYPES.CORE)
                          .filter(**{f"{role}__isnull": False}).distinct().to_list()[:5])
    _try("mixed, field first: filter(type=..., role=...)",
         lambda: ln.Record.filter(**{"type__name": TYPES.CORE, f"{role}__isnull": False})
                          .distinct().to_list()[:5])
    _try("mixed, feature first: filter(role=..., type=...)",
         lambda: ln.Record.filter(**{f"{role}__isnull": False, "type__name": TYPES.CORE})
                          .distinct().to_list()[:5])
    _try("join form: filter(values_artifact__feature__name=role)",
         lambda: ln.Record.filter(type__name=TYPES.CORE)
                          .filter(values_artifact__feature__name=role)
                          .distinct().to_list()[:5])
    _try("state join (review.list_by_state idiom)",
         lambda: ln.Record.filter(type__name=TYPES.CORE)
                          .filter(values_json__feature__name=FEATURES.PIPELINE_STATE)
                          .distinct().to_list()[:5])
    _try("categorical value that matches no label",
         lambda: ln.Record.filter(type__name=TYPES.CORE)
                          .filter(**{FEATURES.PARENT_ROI: "::no-such-roi::"})
                          .distinct().to_list())


# Geometry parquets
def probe_geometry(ln, limit: int) -> None:
    """Does `scale_factor` survive in the parquet schema metadata of registered geometry?"""
    print("\n[3] geometry round-trip: does scale_factor survive to_parquet?")
    try:
        import pyarrow.parquet as pq
    except ImportError:
        record("pyarrow available", "SKIP", "install clinicalomx[extract] to run this probe")
        return

    arts = [a for a in ln.Artifact.filter(suffix=".parquet").order_by("-created_at").to_list()
            if a.key][:limit]
    if not arts:
        record("geometry artifacts found", "NONE", "no .parquet artifacts registered")
        return

    carried, bare, unreadable = [], [], []
    for art in arts:
        try:
            meta = pq.read_schema(art.path).metadata or {}
        except Exception as exc:  # noqa: BLE001 - an unreadable file is itself the finding
            unreadable.append(f"{art.key} ({type(exc).__name__})")
            continue
        blob = b" ".join(list(meta.keys()) + list(meta.values())).lower()
        (carried if b"scale_factor" in blob else bare).append(art.key)

    record("carries scale_factor", f"{len(carried)}", ", ".join(carried[:3]))
    record("MISSING scale_factor", f"{len(bare)}", ", ".join(bare[:3]))
    if unreadable:
        record("unreadable", f"{len(unreadable)}", ", ".join(unreadable[:3]))
    if bare:
        print("      A builder MUST raise on these rather than defaulting to 1.0: a lost scale")
        print("      factor makes every coordinate wrong by an unknown factor, with no error.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tma", help="TMA Record name to probe; default is the first by name")
    ap.add_argument("--role", help="role feature for probe 2; default is the first declared")
    ap.add_argument("--geometry-limit", type=int, default=10,
                    help="how many .parquet artifacts probe 3 opens (default 10)")
    args = ap.parse_args()

    import lamindb as ln

    print(f"instance: {ln.setup.settings.instance.slug}")
    tma = _first_tma(ln, args.tma)
    for probe, arg in ((probe_parent_join, tma), (probe_pushdown, args.role),
                       (probe_geometry, args.geometry_limit)):
        try:
            probe(ln, arg)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001 - one broken probe must not hide the other two
            record(probe.__name__, "ERROR", "see traceback below")
            traceback.print_exc()

    print("\nsummary")
    for name, verdict, detail in VERDICTS:
        print(f"  {verdict:12} {name}" + (f"  {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
