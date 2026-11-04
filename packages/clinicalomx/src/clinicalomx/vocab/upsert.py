"""Reconciliation primitives used by every vocabulary step.

Never delete and recreate a type: categorical features pin to its UID, so a new UID breaks them.
Reuse what exists and repair a drifted categorical dtype in place.

Every mutation goes through :class:`Reconciler`, which records a :class:`Change` for each.
``dry_run=True`` writes nothing. On a fresh instance, a feature whose dtype references a type not
yet created plans as ``pending`` rather than ``create``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import lamindb as ln

from clinicalomx.features import upsert_values

Action = Literal["noop", "create", "repair", "pending"]


@dataclass(frozen=True)
class Change:
    kind: str
    name: str
    action: Action
    detail: str = ""

    def __str__(self) -> str:
        tail = f"  ({self.detail})" if self.detail else ""
        return f"{self.action.upper():<8} {self.kind:<9} {self.name}{tail}"


class _Pending:
    """Stand-in for an object that ``plan`` would create but cannot resolve."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<pending {self.name!r}>"


@dataclass
class Reconciler:
    """Converge the instance toward the declared state, recording every change.

    ``dry_run`` resolves and diffs but never writes.
    """

    dry_run: bool = False
    changes: list[Change] = field(default_factory=list)

    # Report.
    @property
    def pending_changes(self) -> list[Change]:
        """Changes that would alter the instance: everything but ``noop``."""
        return [c for c in self.changes if c.action != "noop"]

    def _log(self, kind: str, name: str, action: Action, detail: str = "") -> None:
        self.changes.append(Change(kind, name, action, detail))

    # Primitives.
    def storage(self, root: str, **kwargs: Any):
        """Get-or-create an ``ln.Storage``, matched by root."""
        existing = ln.Storage.filter(root=root).one_or_none()
        if existing is not None:
            self._log("storage", root, "noop")
            return existing
        self._log("storage", root, "create", kwargs.get("description", ""))
        if self.dry_run:
            return _Pending(root)
        return ln.Storage(root, **kwargs).save()

    def type(self, name: str, **kwargs: Any):
        """Get-or-create a type record (``is_type=True``), matched by name. Never deletes.

        A changed ``description`` on an existing type is reported, not applied.
        """
        existing = ln.Record.filter(name=name, is_type=True).one_or_none()
        if existing is not None:
            want = kwargs.get("description")
            if want and existing.description != want:
                self._log("type", name, "noop", "description differs (not applied)")
            else:
                self._log("type", name, "noop")
            return existing
        self._log("type", name, "create", kwargs.get("description", ""))
        if self.dry_run:
            return _Pending(name)
        return ln.Record(name=name, is_type=True, **kwargs).save()

    def feature(self, name: str, dtype: Any, **kwargs: Any):
        """Get-or-create a feature; repair a drifted dtype in place.

        In-place repair is unsafe once records carry values under the old dtype; see README,
        "When repair becomes unsafe".
        """
        from .dataio import LiteralDtype

        if isinstance(dtype, _Pending):
            self._log("feature", name, "pending", f"dtype -> {dtype.name} (not yet created)")
            return _Pending(name)

        want = declared_dtype(dtype)

        existing = ln.Feature.filter(name=name).one_or_none()
        if existing is None:
            if isinstance(dtype, LiteralDtype):
                # A dangling pin cannot be constructed: features.csv is stale. Re-dump it.
                self._log("feature", name, "pending",
                          f"dangling pin {dtype} declared but feature absent; re-dump")
                return _Pending(name)
            self._log("feature", name, "create", want)
            if self.dry_run:
                return _Pending(name)
            return ln.Feature(name=name, dtype=dtype, **kwargs).save()

        if existing._dtype_str == want:
            self._log("feature", name, "noop")
            return existing

        if isinstance(dtype, LiteralDtype):
            # Declared state still names the old dangling pin: report drift, write nothing.
            self._log("feature", name, "repair",
                      f"{existing._dtype_str} != declared {want} (dangling pin, not applied)")
            return existing

        self._log("feature", name, "repair", f"{existing._dtype_str} -> {want}")
        if self.dry_run:
            return existing
        existing._dtype_str = want  # private API: lamindb version is pinned; see tests
        existing.save()
        return existing

    def record(self, name: str, type_record: Any, **kwargs: Any):
        """Get-or-create a plain (non-type) record of ``type_record``."""
        if isinstance(type_record, _Pending):
            self._log("record", name, "pending", f"type -> {type_record.name}")
            return _Pending(name)

        existing = ln.Record.filter(name=name, type=type_record).one_or_none()
        if existing is not None:
            self._log("record", name, "noop")
            return existing
        self._log("record", name, "create", f"type={type_record.name}")
        if self.dry_run:
            return _Pending(name)
        return ln.Record(name=name, type=type_record, **kwargs).save()

    def values(self, record: Any, values: dict[str, Any]) -> None:
        """Set feature values on a record, per key, leaving its other values intact."""
        if isinstance(record, _Pending):
            self._log("values", record.name, "pending", ", ".join(values))
            return
        self._log("values", str(record.name), "noop" if self.dry_run else "repair",
                  ", ".join(values))
        if not self.dry_run:
            upsert_values(record.features, values)


def declared_dtype(dtype: Any) -> str:
    """The stored form of a declared dtype: ``cat[bionty.Protein]``, ``str``, and so on.

    A :class:`LiteralDtype` is compared as text and never resolved. A plain ``str`` is parsed
    without importing the registry: ``cat[Record]`` is accepted, bare ``Record`` raises. Anything
    else is a registry class or field, and is serialised.
    """
    from .dataio import LiteralDtype

    # LiteralDtype subclasses str, so it has to be tested first.
    if isinstance(dtype, LiteralDtype):
        return str(dtype)
    if isinstance(dtype, str):
        from lamindb.models.feature import parse_dtype

        parse_dtype(dtype)
        return dtype
    return _serialize(dtype)


def _serialize(dtype: Any) -> str:
    from lamindb.models.feature import serialize_dtype

    return serialize_dtype(dtype)
