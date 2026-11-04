"""Registering an assembled output. The only module in ``extract/`` allowed to write.

Registration goes through ``idempotency.materialize`` and ``roi.annotate``, never a raw
``.save()``. A merged output is annotated onto its parent ROI only, never onto each child.
"""
from __future__ import annotations

from clinicalomx import roi as roi_ops
from clinicalomx.governance import folder_of, permits_derive
from clinicalomx.idempotency import materialize
from clinicalomx.names import FEATURES


class PublishDenied(RuntimeError):
    """Governance forbids writing a derivative here. Names the folder."""


def check_governance(keys) -> dict[str, str]:
    """``{key: verdict}``. A new output folder is denied until a policy declares it."""
    out = {}
    for key in keys:
        out[key] = "derive" if permits_derive(key) else f"DENIED (folder {folder_of(key)!r})"
    return out


def register_outputs(plan, built, *, recipe, snapshot=None) -> int:
    """Register each built output as an ROI-anchored Artifact. Idempotent. Returns how many.

    A failure on one output does not stop the rest.
    """
    import lamindb as ln


    if recipe.publish_role is None:
        raise PublishDenied(
            f"recipe {recipe.name!r} declares no publish_role, so it has no role to register "
            f"under. Export it to a path instead, or declare one on the recipe.")

    verdicts = check_governance(b.output for b in built)
    denied = {k: v for k, v in verdicts.items() if v.startswith("DENIED")}
    if denied:
        raise PublishDenied(
            "governance forbids writing a derivative to "
            + "; ".join(f"{k} - {v}" for k, v in sorted(denied.items())[:3])
            + ". Declare the folder through the `clinicalomx.governance` entry point, or "
              "register_policies() if this consumer is not pip-installed. Deny-by-default means "
              "an undeclared folder and a forbidden one look the same from here.")

    n = 0
    for item in built:
        target = ln.Record.filter(name=item.roi_name, is_type=False).first()
        if target is None:
            continue

        def _compute(path=item.output):
            return {path: ln.Artifact(path, key=path).save()}

        def _reconcile(arts, anchor=target):
            for art in arts.values():
                # annotate() is idempotent both ways.
                roi_ops.annotate(anchor, art, recipe.publish_role)
                art.features.add_values({FEATURES.COORDINATE_SPACE: recipe.coordinate_space})

        materialize([item.output], compute=_compute, reconcile=_reconcile)
        item.registered = True
        n += 1
    return n
