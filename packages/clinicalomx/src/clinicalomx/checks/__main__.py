from __future__ import annotations

import argparse
import sys

from . import all_checks, run_checks, summarise
from .extract import extract_checks
from .ontology import ontology_checks
from .roi import framework_checks


def cmd_run(args) -> int:
    checks = [c for c in all_checks() if args.only is None or args.only in c.name]
    if not checks:
        print(f"no checks match {args.only!r}")
        return 1
    print(f"running {len(checks)} check(s), include_storage={args.include_storage}\n")
    results = run_checks(checks, include_storage=args.include_storage)
    report, has_errors = summarise(results, max_examples=args.max_examples)
    print(report)
    return 1 if has_errors else 0


def cmd_list(args) -> int:
    from . import discover
    groups = [("clinicalomx", [*framework_checks(), *extract_checks(), *ontology_checks()]),
              *sorted(discover().items())]
    for owner, checks in groups:
        print(f"\n{owner} ({len(checks)} check(s))")
        for c in checks:
            flags = " [storage]" if c.needs_storage else ""
            print(f"  {c.severity.upper():5} {c.name}{flags}")
            if c.description:
                print(f"        {c.description}")
    total = sum(len(c) for _o, c in groups)
    print(f"\n{total} check(s) across {len(groups)} declaring package(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clinicalomx-checks", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run every declared check; non-zero on an error-severity fail")
    run.set_defaults(fn=cmd_run)
    run.add_argument("--include-storage", action="store_true",
                     help="also run checks that read artifact bytes (needs byte access)")
    run.add_argument("--only", metavar="SUBSTRING",
                     help="run only checks whose name contains SUBSTRING, e.g. 'roi.'")
    run.add_argument("--max-examples", type=int, default=5, metavar="N",
                     help="failing instances to print per check (default 5)")

    lst = sub.add_parser("list", help="show what would run, and who declared it (no database)")
    lst.set_defaults(fn=cmd_list)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
