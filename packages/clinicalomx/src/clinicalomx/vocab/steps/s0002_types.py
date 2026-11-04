"""Type records (``ln.Record(is_type=True)``), from ``data/types.yaml``.

``scripts/dump_instance.py`` generates the file. Parents resolve by name. Each type's observed UID
is recorded but never written: a mismatch means the type was deleted and recreated, which breaks
every categorical feature pinned to it.
"""

from __future__ import annotations

from ..dataio import types

NAME = "types"


def apply(rec, ctx: dict) -> None:
    declared = types()
    by_name = {t["name"]: t for t in declared}
    resolved: dict = {}

    def resolve(name: str):
        if name in resolved:
            return resolved[name]
        spec = by_name[name]
        kwargs = {"description": spec.get("description", "")}
        parent = spec.get("type")
        if parent:
            kwargs["type"] = resolve(parent)
        obj = rec.type(name, **kwargs)
        _check_uid(rec, obj, spec)
        resolved[name] = obj
        return obj

    for name in by_name:
        resolve(name)
    ctx["types"] = resolved


def _check_uid(rec, obj, spec) -> None:
    """Warn if a type's UID differs from the recorded one."""
    expected = spec.get("uid")
    actual = getattr(obj, "uid", None)
    if expected and actual and expected != actual:
        rec._log(
            "type", spec["name"], "repair",
            f"UID DRIFT {expected} -> {actual}: categorical features pinned to the "
            "old UID are now dangling",
        )
