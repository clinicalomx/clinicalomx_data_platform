"""The ``utils-recover-annotated-images`` deployment: the recovery tool as a flow.

Calls the functions in ``recover_annotated_images`` unchanged.

- ``apply=False`` runs ``check``: P1-P7 and the verdict. Writes nothing.
- ``apply=True`` runs the checks, applies only on ``CLEAR``, then checks again.
- ``accept_by_name_links=True`` is ``check --accept-by-name-links``. ``apply`` never implies it.

A verdict other than ``CLEAR`` fails the run with that line. Re-runnable: over an existing
Collection it writes nothing. Imports other than prefect stay in the flow body.
"""
from __future__ import annotations

from prefect import flow


@flow(name="recover-annotated-images", log_prints=True)
def recover_annotated_images(apply: bool = False, accept_by_name_links: bool = False) -> str:
    """Check, and with ``apply`` rebuild, ``clinical-roi/core/annotated_images`` v1.

    apply: write the Collection when the checks read CLEAR.
    accept_by_name_links: accept P6's labels linked to Records matched by name alone.
    Returns the verdict line.
    """
    from croi.objectives.tma_core import recover_annotated_images as rec

    storage_root, labels_dir, labels_prefix = rec.locate()

    def checked() -> str:
        results = rec.run_checks(storage_root, labels_dir, labels_prefix)
        code = rec.print_results(results, accept_by_name_links)
        line = rec.verdict(results, accept_by_name_links)[1]
        if code:
            raise RuntimeError(line)
        return line

    line = checked()
    if not apply:
        return line
    print()
    rec.apply(labels_prefix)
    print("\nchecks after apply:")
    return checked()
