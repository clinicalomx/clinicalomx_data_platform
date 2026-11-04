"""``clinicalomx-extract`` - find an ROI, see what it can give you, plan it, build it.

    clinicalomx-extract recipes                    # the catalogue           no database
    clinicalomx-extract docs [--check PATH]        # the CI-checked reference  no database
    clinicalomx-extract find "core R2C4 in TMA0 of the Arutha slide"
    clinicalomx-extract describe <uid|name> [--include-storage] [--inspect-stores]
    clinicalomx-extract plan --roi "<name>" --target OME-TIFF --dest ./out [--verify-placement]
    clinicalomx-extract plan --roi "<slide>:TMA0" --recipe ometiff.tma.mosaic --downsample 8 ...
    clinicalomx-extract assemble --plan plan.json --yes

`plan` writes nothing and exits 1 only when something blocks; a degraded plan exits 0. `assemble`
re-plans and refuses to build if the token changed. Never calls ``lamin connect``.
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys

from clinicalomx.extract.assemble import AssemblyError, assemble
from clinicalomx.extract.availability import (
    describe,
    inspect_stores,
    summarise,
    variables_addressed,
)
from clinicalomx.extract.docs import render


def cmd_recipes(args) -> int:
    """The catalogue: foundation plus whatever is installed."""
    from clinicalomx.extract import all_recipes, discover, providers

    recipes = [r for r in all_recipes()
               if args.target is None or r.target.lower() == args.target.lower()]
    found = discover()

    if args.json:
        print(json.dumps({
            "recipes": [r.to_dict() for r in recipes],
            "providers": list(providers()),
            "consumer_providers": {k: len(v) for k, v in found.items()},
        }, indent=2))
        return 0

    # Always state the provider count.
    print(f"{len(recipes)} recipe(s) from {len(found) + 1} provider(s): "
          f"{', '.join(providers())}")
    for r in recipes:
        anchor = f" -> {r.publish_role}" if r.publish_role else ""
        print(f"\n  {r.name}  [{r.target}]{anchor}")
        print(f"      ask of {', '.join(r.roi_types)}; layout {r.layout}; owner {r.owner}")
        if r.description:
            print(f"      {r.description}")
        for e in r.elements:
            need = "required" if e.required else "optional"
            print(f"      - {e.name:8} {e.kind:8} {need:8} <- {', '.join(e.roles)}")
    return 0


def cmd_describe(args) -> int:
    """What this ROI can give you, and why not, where not."""
    from clinicalomx.extract.source import default_source

    src = default_source()
    roi = src.entity(uid=args.roi) or src.entity(name=args.roi, order_by="name")
    if roi is None:
        print(f"no ROI Record with uid or name {args.roi!r}")
        return 1

    av = describe(roi, include_storage=args.include_storage)
    # Opt-in: opens one store per artifact.
    inside = _inspect_stores(av, source=src) if args.inspect_stores else {}
    if args.json:
        print(json.dumps({"ok": True, "kind": "availability", "data": av.to_dict(),
                          "stores": {r: c.to_dict() for r, c in inside.items()},
                          "caveats": av.caveats()}, indent=2))
        return 0

    print(summarise(av))
    if inside:
        print(_store_report(av, inside))
    print("\ncaveats")
    for c in av.caveats():
        print(f"  - {c}")
    # Exit 1 only when every recipe is blocked.
    return 1 if all(r.verdict == "blocked" for r in av.recipes) and av.recipes else 0


def _inspect_stores(av, *, source):
    return inspect_stores(av, source=source)


def _store_report(av, inside) -> str:
    """What each opened store holds, and which arrays no element addresses."""
    out = ["", "inside one store per artifact (a sample; nothing asserts the others match it)"]
    for role, contents in sorted(inside.items()):
        out.append(f"  {role}  {contents.key}")
        if not contents.ok:
            out.append(f"      cannot list: {contents.error}")
            continue
        addressed = variables_addressed(av.child_elements, role)
        whole = addressed.get("")
        for v in contents.variables:
            by = addressed.get(v.name) or (f"{whole} (whole store)" if whole else "(nothing)")
            shape = " x ".join(str(n) for n in v.shape)
            out.append(f"      {v.name:24} {','.join(v.dims):14} {shape:22} {v.dtype:9} <- {by}")
        for c in contents.coords:
            values = ", ".join(c.values[:6]) + (", ..." if len(c.values) > 6 else "")
            out.append(f"      coord {c.name:18} {','.join(c.dims) or '(scalar)':14} "
                       f"{c.dtype:9} {values}".rstrip())
    return "\n".join(out)


def cmd_find(args) -> int:
    """Match free text against ROI names. Prints candidates; never disambiguates."""
    from clinicalomx.extract.select import find

    res = find(args.query, roi_type=args.type, limit=args.limit)
    if args.json:
        print(json.dumps({"ok": True, "kind": "resolution", "data": res.to_dict(),
                          "caveats": res.caveats()}, indent=2))
        return 0 if res.exact else 1

    print(f"query   {res.query!r}")
    print(f"parsed  {res.parsed}")
    if res.exact:
        print(f"\nEXACT   {res.exact.name}   uid {res.exact.uid}   [{res.exact.roi_type}]")
        return 0
    print(f"\n{len(res.candidates)} candidate(s), {res.tied} tied for best:")
    for c in res.candidates:
        print(f"  {c.score:.2f}  {c.name}   [{c.roi_type}]  uid {c.uid}")
        print(f"        {c.why}")
    for c in res.caveats():
        print(f"\n  {c}")
    return 1


def _request_from(args):
    from clinicalomx.extract.request import Destination, ExtractRequest, ROISelector

    if args.request:
        return ExtractRequest.from_yaml(args.request)
    if not (args.roi and args.target and args.dest):
        raise SystemExit("give --request FILE, or all of --roi, --target and --dest")
    return ExtractRequest(
        roi=ROISelector(name=args.roi),
        target=args.target,
        recipe=args.recipe,
        layout=args.layout,
        destination=Destination(kind="artifact" if args.register else "path",
                                **({"key": args.dest} if args.register else {"path": args.dest})),
        strictness=args.strictness,
        include_storage=args.include_storage,
        verify_placement=getattr(args, "verify_placement", False),
        downsample=getattr(args, "downsample", 1),
    )


def cmd_plan(args) -> int:
    """Resolve a request against the instance. Writes nothing."""
    from clinicalomx.extract.plan import plan

    try:
        result = plan(_request_from(args))
    except (LookupError, ValueError) as exc:
        print(f"cannot plan: {exc}")
        return 2

    if args.json:
        print(json.dumps({"ok": True, "kind": "plan", "data": result.to_dict(),
                          "caveats": result.caveats()}, indent=2))
    else:
        report, blocked = result.summarise()
        print(report)
        print("\ncaveats")
        for c in result.caveats():
            print(f"  - {c}")
        if args.out:
            pathlib.Path(args.out).write_text(json.dumps(result.to_dict(), indent=2))
            print(f"\nwrote {args.out}; assemble it with --plan {args.out}")
    # Exit 1 only when blocked.
    return 1 if result.blocked else 0


def cmd_assemble(args) -> int:
    """Build a plan that was already produced."""

    from clinicalomx.extract.plan import plan as make_plan

    if args.plan:
        stored = json.loads(pathlib.Path(args.plan).read_text())
        print(f"re-planning from the request in {args.plan} (token was {stored.get('token')})")
        result = make_plan(_request_model(stored["request"]))
        if result.token != stored.get("token"):
            print(f"REFUSING: the instance has changed since that plan was written.\n"
                  f"  planned  {stored.get('token')}\n  now      {result.token}\n"
                  f"Re-run `plan` and review the difference before building.")
            return 2
    else:
        result = make_plan(_request_from(args))

    report, _blocked = result.summarise()
    print(report)
    if not args.yes:
        print("\nNothing built. Re-run with --yes once the plan above is what you want.")
        return 0
    try:
        out = assemble(result, confirm=True, force_stale=args.force_stale)
    except AssemblyError as exc:
        print(f"\ncannot assemble: {exc}")
        return 2
    print(f"\nbuilt {out['n_built']}, registered {out['n_registered']}")
    for b in out["built"]:
        print(f"  {b['output']}\n      {b['detail']}")
    for f in out["failed"]:
        print(f"  FAILED  {f}")
    return 1 if out["failed"] else 0


def _request_model(payload: dict):
    from clinicalomx.extract.request import ExtractRequest

    return ExtractRequest.model_validate(payload)


def cmd_docs(args) -> int:
    """Render the reference, or check a committed copy against it."""
    text = render()
    if not args.check:
        print(text, end="")
        return 0

    path = pathlib.Path(args.check)
    current = path.read_text() if path.exists() else ""
    if current == text:
        print(f"OK: {path} matches the declared recipes.")
        return 0
    print(f"STALE: {path} does not match the declared recipes.")
    print("".join(difflib.unified_diff(
        current.splitlines(keepends=True), text.splitlines(keepends=True),
        fromfile=str(path), tofile="rendered"))[:4000])
    print(f"\nRegenerate: python -m clinicalomx.extract docs > {path}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="clinicalomx-extract", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("recipes", help="list the downstream products you can ask for")
    p.add_argument("--target", help="only this target, e.g. SpatialData")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_recipes)

    p = sub.add_parser("describe", help="what a given ROI can be turned into")
    p.add_argument("roi", help="ROI Record uid or name, e.g. '<slide>:TMA0'")
    p.add_argument("--include-storage", action="store_true",
                   help="probe artifact bytes; without it 'missing' is never reported")
    p.add_argument("--inspect-stores", action="store_true",
                   help="open ONE store per artifact and list its arrays from metadata; shows "
                        "the ones no element addresses. Needs clinicalomx[extract]")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser("find", help="match free text against ROI names")
    p.add_argument("query", help='e.g. "core R2C4 in TMA0 of the Arutha slide"')
    p.add_argument("--type", help="only this ROI subtype")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_find)

    for name, fn, helptext in (("plan", cmd_plan, "resolve a request; writes nothing"),
                               ("assemble", cmd_assemble, "build a plan that was reviewed")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--request", metavar="FILE", help="a request as yaml")
        p.add_argument("--roi", help="ROI Record name")
        p.add_argument("--target", help="SpatialData | OME-TIFF | OME-Zarr")
        p.add_argument("--recipe", help="pin one recipe by name")
        p.add_argument("--layout", choices=("single", "merged", "per_child"))
        p.add_argument("--dest", help="output directory, or artifact key prefix with --register")
        p.add_argument("--register", action="store_true",
                       help="register outputs as ROI-anchored Artifacts instead of writing locally")
        p.add_argument("--strictness", choices=("strict", "degrade", "best_effort"),
                       default="strict")
        p.add_argument("--include-storage", action="store_true")
        p.add_argument("--downsample", type=int, default=1, metavar="N",
                       help="take every Nth pixel when compositing a mosaic. A full-resolution "
                            "TMA mosaic is tens of GB; this is how you get an overview.")
        p.add_argument("--verify-placement", action="store_true",
                       help="place from the origin each image RECORDS, and report any metadata "
                            "strategy that disagrees. Reads zarr metadata, never pixels.")
        p.add_argument("--json", action="store_true")
        if name == "plan":
            p.add_argument("--out", metavar="FILE", help="write the plan for `assemble --plan`")
        else:
            p.add_argument("--plan", metavar="FILE", help="a plan written by `plan --out`")
            p.add_argument("--yes", action="store_true", help="build it")
            p.add_argument("--force-stale", action="store_true",
                           help="build even though the index may have moved on")
        p.set_defaults(fn=fn)

    p = sub.add_parser("docs", help="render the recipe reference")
    p.add_argument("--check", metavar="PATH", help="compare PATH against the render; 1 if stale")
    p.set_defaults(fn=cmd_docs)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
