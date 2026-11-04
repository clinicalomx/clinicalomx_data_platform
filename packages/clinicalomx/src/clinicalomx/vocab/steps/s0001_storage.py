"""Storage locations, from ``data/storages.yaml``. The roots are site values from site.yaml."""

from __future__ import annotations

from ..dataio import storages

NAME = "storage"


def apply(rec, ctx: dict) -> None:
    """Register the declared storage locations, and optionally set the default.

    ``ctx["default_storage"]`` names the storage to set as ``ln.settings.storage``; unset leaves
    the instance default alone.
    """
    default = ctx.get("default_storage")
    out = {}
    for entry in storages():
        out[entry["key"]] = rec.storage(
            entry["root"],
            description=entry.get("description", ""),
            host=entry.get("host"),
        )

    if default and default in out and not rec.dry_run:
        import lamindb as ln  # importing lamindb connects to the instance

        target = out[default]
        if hasattr(target, "path"):
            ln.settings.storage = target.path
    ctx["storages"] = out
