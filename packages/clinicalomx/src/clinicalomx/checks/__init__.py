"""Declarative data tests over the instance."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from clinicalomx.snapshot import Snapshot

__all__ = [
    "Check", "Result", "Severity", "Snapshot", "ENTRY_POINT_GROUP",
    "all_checks", "discover", "run_checks", "summarise",
]

Severity = Literal["error", "warn"]

#: Entry-point group under which consumers advertise a ``() -> list[Check]`` callable.
ENTRY_POINT_GROUP = "clinicalomx.checks"

@dataclass(frozen=True)
class Check:
    """One declared invariant. ``test`` takes the :class:`Snapshot` and returns the failing
    instances as strings; an empty result is a pass."""

    name: str
    severity: Severity
    test: Callable[[Snapshot], Iterable[str]]
    description: str = ""
    #: True when the check reads artifact bytes, so it cannot run on a metadata-only control plane.
    needs_storage: bool = False

@dataclass
class Result:
    check: Check
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def discover() -> dict[str, list[Check]]:
    """Every check set advertised by an installed distribution, keyed by entry-point name.

    A consumer that fails to load is reported and skipped.
    """
    from importlib.metadata import entry_points

    found: dict[str, list[Check]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            checks = list(ep.load()())
        except Exception as exc:  # noqa: BLE001 - one broken consumer must not break the run
            print(f"  WARNING  could not load checks {ep.name!r}: {exc}")
            continue
        bad = [c for c in checks if not isinstance(c, Check)]
        if bad:
            print(f"  WARNING  {ep.name!r} yielded {len(bad)} non-Check object(s); skipped")
        found[ep.name] = [c for c in checks if isinstance(c, Check)]
    return found


def all_checks() -> list[Check]:
    """The framework's own invariants plus every installed consumer's."""
    from clinicalomx.checks.extract import extract_checks
    from clinicalomx.checks.ontology import ontology_checks
    from clinicalomx.checks.roi import framework_checks

    out = [*framework_checks(), *extract_checks(), *ontology_checks()]
    for checks in discover().values():
        out.extend(checks)
    return out


def run_checks(checks: Iterable[Check], *, snapshot: Snapshot | None = None,
               include_storage: bool = False, logger=None) -> list[Result]:
    """Run every check against one shared snapshot and return the results.

    ``include_storage=False`` skips checks that read bytes. A check that raises becomes a failure.
    """
    snap = snapshot if snapshot is not None else Snapshot()
    out: list[Result] = []
    for check in checks:
        if check.needs_storage and not include_storage:
            if logger:
                logger.info(f"skip (needs storage): {check.name}")
            continue
        try:
            failures = list(check.test(snap))
        except Exception as exc:  # noqa: BLE001 - one broken check must not mask the rest
            failures = [f"check raised {type(exc).__name__}: {exc}"]
        out.append(Result(check, failures))
        if logger:
            mark = "ok  " if not failures else check.severity.upper()
            logger.info(f"{mark} {check.name}: {len(failures)} failure(s)")
    return out


def summarise(results: list[Result], *, max_examples: int = 5) -> tuple[str, bool]:
    """Human-readable report plus whether any ``error``-severity check failed."""
    lines, failed = [], False
    for r in sorted(results, key=lambda r: (r.check.severity != "error", r.check.name)):
        if r.passed:
            lines.append(f"  ok    {r.check.name}")
            continue
        if r.check.severity == "error":
            failed = True
        lines.append(f"  {r.check.severity.upper():5} {r.check.name}: {len(r.failures)}")
        for f in r.failures[:max_examples]:
            lines.append(f"            {f}")
        if len(r.failures) > max_examples:
            lines.append(f"            ... and {len(r.failures) - max_examples} more")
    n_err = sum(1 for r in results if not r.passed and r.check.severity == "error")
    n_warn = sum(1 for r in results if not r.passed and r.check.severity == "warn")
    header = f"{len(results)} check(s): {n_err} error(s), {n_warn} warning(s)"
    return header + "\n" + "\n".join(lines), failed
