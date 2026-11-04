#!/usr/bin/env python3
"""Render a schema apply into a page the operator can review and annotate.

    make schema-review
    make schema-review LAMIN_HOME=/home/ubuntu/.lamin        # on the control-plane VM

Three panels: declared versus landed, sampled rows checked by lamin_api's validators, and the
verdict. The page writes nothing. Exits zero whenever it wrote a page, drift included; make
schema-check is the gate. Counts go to stderr.

Only collect.py reaches the database, through ../explorer/lamin_api.py imported inside a function.
compare.py and render.py are pure. Add a new "not checked" count to compare.gap_report, and change
which diff sections make up the row list only in compare.schema_rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compare  # noqa: E402
import declared as declared_mod  # noqa: E402
import render  # noqa: E402

DEFAULT_ROWS = 20


def build(declared: dict, landed: dict, samples: list[dict], kind, scalar,
          dropped_registries: list[str] | None = None) -> dict:
    """The finished comparison, ready to render. Every read has already happened.

    validated holds one compare.validate_sample result per sample. dropped_registries lists those
    collect's cap did not sample.
    """
    type_uids = {t["name"]: t.get("uid", "") for t in landed["types"]}
    registries = compare.registry_spellings(landed.get("registries") or [])
    specs = {f["name"]: f for f in landed["features"]}

    validated = [compare.validate_sample(sample, specs, kind, scalar) for sample in samples]

    return {
        "declared": declared,
        "landed": landed,
        "samples": samples,
        "validated": validated,
        "dropped_registries": list(dropped_registries or []),
        "schema": {
            "features": compare.diff_features(declared["features"], landed["features"],
                                              type_uids, registries),
            "types": compare.diff_types(declared["types"], landed["types"]),
            "storages": compare.diff_storages(declared["storages"], landed["storages"]),
        },
    }


def schema_rows(context: dict) -> list[dict]:
    return compare.schema_rows(context["schema"])


def report(context: dict) -> dict:
    """compare.gap_report for this context."""
    return compare.gap_report(schema_rows(context), context["samples"],
                              context.get("validated") or [],
                              context.get("dropped_registries") or [])


def summarise(context: dict) -> str:
    """The stderr line, with the same counts as the page."""
    rows = schema_rows(context)
    gaps = report(context)
    return (f"{len(compare.failures(rows))} schema failures, "
            f"{len(gaps['unresolved'])} declared dtypes unresolved, "
            f"{len(gaps['retired'])} deprecated names absent, "
            f"{len(gaps['problems'])} flagged values, "
            f"{gaps['judged']} cells judged, {len(gaps['skipped'])} left unjudged, "
            f"{len(gaps['absent_columns'])} merged features with no column, "
            f"{len(gaps['unjudged_registries'])} registries with no cell judged, "
            f"{len(context['samples'])} registries sampled, "
            f"{len(gaps['dropped_registries'])} named registries not sampled")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schema-review", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="schema-review.html", metavar="PATH",
                        help="where to write the page (default: %(default)s)")
    parser.add_argument("--declared", metavar="DIR",
                        help="the clinicalomx data directory, if it is not the installed one")
    parser.add_argument("--registry", action="append", metavar="NAME",
                        help="registry to sample; repeatable, defaults to Artifact and Record")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, metavar="N",
                        help="rows sampled per registry (default: %(default)s)")
    args = parser.parse_args(argv)

    import collect

    # An empty declaration is refused.
    try:
        data_dir = Path(args.declared) if args.declared else declared_mod.find_data_dir()
        declaration = declared_mod.load(data_dir, declared_mod.package_version())
    except declared_mod.NotFound as exc:
        print(f"cannot read the declaration: {exc}", file=sys.stderr)
        return 2

    # A disconnected or misshapen instance is refused.
    try:
        landed = collect.landed()
        chosen, dropped = collect.choose_registries(landed["registries"], args.registry)
        samples = [collect.sample(name, args.rows) for name in chosen]
        kind, scalar = collect.rules()
    except (RuntimeError, LookupError, ValueError) as exc:
        print(f"cannot read the instance: {exc}", file=sys.stderr)
        return 2

    context = build(declaration, landed, samples, kind, scalar, dropped)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render.render(context), encoding="utf-8")
    print(f"wrote {out}: {summarise(context)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
