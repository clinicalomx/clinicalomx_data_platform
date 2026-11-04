"""``plan | apply | check | audit``: seed and verify the instance vocabulary.

    python -m clinicalomx.vocab plan
    python -m clinicalomx.vocab apply --default-storage hpc_file
    python -m clinicalomx.vocab check         # CI gate: non-zero if the foundation is unmet
    python -m clinicalomx.vocab audit         # what is missing, and what is undeclared

``data/`` is the foundation: what the framework reads or writes plus shared conventions. ``plan``,
``apply`` and ``check`` only add and never remove. ``--with-consumers`` widens ``plan`` and
``apply`` to every installed consumer's vocabulary. ``audit`` reports ownership.

Run against an already connected instance (``make db-shell``). This never calls ``lamin connect``.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from ..names import ALL_FEATURES, ALL_TYPES, PROJECT
from . import ledger
from .dataio import features as declared_features
from .dataio import storages as declared_storages
from .dataio import types as declared_types
from .docs import render
from .ontology import reconcile
from .steps import STEPS

# `import lamindb` connects, so lamindb imports are deferred into each command; `--help` needs none.


def run(dry_run: bool, default_storage: str | None = None, with_consumers: bool = False):
    """Execute every step in order against one Reconciler.

    ``with_consumers`` also reconciles every installed consumer's vocabulary. Off by default.
    """
    from .upsert import Reconciler

    rec = Reconciler(dry_run=dry_run)
    ctx: dict = {"default_storage": default_storage}
    for step in STEPS:
        step.apply(rec, ctx)
    ledger.stamp(rec)

    if with_consumers:
        from .registry import discover

        for owner, vocab in sorted(discover().items()):
            rec._log("consumer", owner, "noop",
                     f"{len(vocab.features)} feature(s), {len(vocab.types)} type(s)")
            for decl in vocab.types.values():
                rec.type(decl.name, description=decl.description)
            for decl in vocab.features.values():
                rec.feature(decl.name, decl.dtype, description=decl.description or None)
            if vocab.sources:
                for entity, spec in vocab.sources.items():
                    reconcile(rec, entity, spec)
    return rec


def _report(rec, *, verbose: bool) -> None:
    for change in rec.changes if verbose else rec.pending_changes:
        print(f"  {change}")
    n = len(rec.pending_changes)
    print(f"\n{n} change(s) pending, {len(rec.changes) - n} already in place.")


def cmd_plan(args) -> int:
    rec = run(dry_run=True, default_storage=args.default_storage,
              with_consumers=args.with_consumers)
    print(f"declared vocabulary : {ledger.content_hash()}")
    print(f"instance is at      : {ledger.applied_version() or '(never seeded)'}\n")
    _report(rec, verbose=args.verbose)
    return 0


def cmd_check(args) -> int:
    """CI gate: fails on any declared feature that is missing or whose dtype has drifted."""
    rec = run(dry_run=True, default_storage=None)
    _report(rec, verbose=args.verbose)
    if rec.pending_changes:
        print("\nBASE UNMET: the instance is missing or has drifted from declared vocabulary.")
        return 1
    print("\nOK: the instance contains the declared base.")
    return 0


def cmd_apply(args) -> int:
    import lamindb as ln


    ln.Project(name=PROJECT).save()
    ln.track(project=PROJECT)
    try:
        rec = run(dry_run=False, default_storage=args.default_storage,
                  with_consumers=args.with_consumers)
        _report(rec, verbose=args.verbose)
        print(f"\napplied vocabulary {ledger.content_hash()}")
    finally:
        ln.finish()
    return 0


def cmd_docs(args) -> int:
    """Render the vocabulary reference, or with ``--check`` verify the committed one. No database.

    Foundation only, so the output does not depend on what is installed.
    """

    text = render()
    if not args.check:
        print(text, end="")
        return 0

    path = Path(args.check)
    current = path.read_text() if path.exists() else ""
    if current == text:
        print(f"OK: {path} matches the declared vocabulary.")
        return 0
    print(f"STALE: {path} does not match the declared vocabulary.\n"
          f"Regenerate with:  python -m clinicalomx.vocab docs > {path}")
    diff = difflib.unified_diff(current.splitlines(), text.splitlines(),
                               fromfile=str(path), tofile="generated", lineterm="", n=1)
    for line in list(diff)[:40]:
        print(f"  {line}")
    return 1


def cmd_audit(args) -> int:
    """Compare the instance with every declaration.

        MISSING     declared but absent  -> fails --strict.
        EXTRA       present, undeclared  -> reported only.

    Look in EXTRA for near-duplicates of declared names, such as ``patientid`` beside
    ``patient_id``.
    """
    import lamindb as ln

    from .registry import FOUNDATION, discover, source_owners

    consumers = discover()
    owner_of: dict[str, str] = {}
    for name in {r["name"].strip() for r in declared_features()} | set(ALL_FEATURES):
        owner_of[name] = FOUNDATION
    for owner, vocab in consumers.items():
        for name in vocab.features:
            owner_of.setdefault(name, owner)

    declared_type_names = {t["name"] for t in declared_types()} | set(ALL_TYPES)
    type_owner: dict[str, str] = dict.fromkeys(declared_type_names, FOUNDATION)
    for owner, vocab in consumers.items():
        for name in vocab.types:
            type_owner.setdefault(name, owner)

    groups = (
        ("feature", owner_of, {f.name for f in ln.Feature.filter()}),
        ("type", type_owner, {t.name for t in ln.Record.filter(is_type=True)}),
        # Storages are matched on root.
        ("storage", {s["root"]: FOUNDATION for s in declared_storages()},
         {s.root for s in ln.Storage.filter()}),
        # An ontology is owned by whoever pins it.
        ("source", source_owners(), _populated_ontologies()),
    )

    n_missing = n_extra = 0
    for kind, declared, live in groups:
        for name in sorted(set(declared) - live):
            print(f"  MISSING  {declared[name]:<22} {kind:<8} {name}")
            n_missing += 1
    for kind, declared, live in groups:
        for name in sorted(live - set(declared)):
            print(f"  extra    {'(unowned)':<22} {kind:<8} {name}")
            n_extra += 1

    if consumers:
        print("\nconsumer vocabularies: " + ", ".join(
            f"{o} ({len(v.features)}f/{len(v.types)}t)" for o, v in sorted(consumers.items())))
    else:
        print("\nno consumer vocabularies installed - everything above is unowned.")
    print(f"{n_missing} missing (the floor), {n_extra} extra.")
    if n_missing and args.strict:
        print("\nFLOOR BROKEN: declared vocabulary is missing. Run apply, or the owner's ensure().")
        return 1
    return 0


def _populated_ontologies() -> set[str]:
    """The bionty entities holding at least one record on the connected instance."""
    import lamindb as ln

    if "bionty" not in ln.setup.settings.instance.modules:
        return set()
    import bionty as bt

    entities = {e.removeprefix("bionty.") for e in bt.Source.filter()
                .values_list("entity", flat=True).distinct()}
    return {e for e in entities if hasattr(bt, e) and getattr(bt, e).filter().exists()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clinicalomx-vocab", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="include no-op changes")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("plan", cmd_plan, "show what would be added to reach the base (no writes)"),
        ("apply", cmd_apply, "add whatever the base is missing"),
        ("check", cmd_check, "exit non-zero if the base is unmet or has drifted"),
        ("audit", cmd_audit, "what the base misses, and what exists above it"),
        ("docs", cmd_docs, "render the vocabulary reference (no database needed)"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(fn=fn)
        if name in {"plan", "apply"}:
            sp.add_argument("--default-storage", metavar="KEY",
                            help="storages.yaml key to set as ln.settings.storage")
            sp.add_argument("--with-consumers", action="store_true",
                            help="also reconcile every installed consumer's Vocabulary "
                                 "(use when provisioning a fresh instance)")
        if name == "docs":
            sp.add_argument("--check", metavar="PATH",
                            help="compare PATH against the render; non-zero if stale")
        if name == "audit":
            sp.add_argument("--strict", action="store_true",
                            help="exit non-zero when DECLARED vocabulary is missing "
                                 "(extras never fail)")

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
