"""Governance policy values for this instance's storage roots.

One table covers the source root (raw scans) and the output root (pipeline derivatives). A folder
name must be unique across both roots. The policy type and its resolution live in
:mod:`clinicalomx.governance`. This table reaches it through the ``clinicalomx.governance`` entry
point and :func:`register`.

The flags form a ladder. Each one requires the one before it:

``readable``  the bytes may be opened, for example to check a qptiff for corruption.
``ingest``    the scans may be registered as Artifacts.
``derive``    bgnorm outputs and other derivatives may be produced and stored.
``training``  human-corrected labels may enter a training-reference Collection
              (``harvest-feedback``), which feeds model retraining.

A ``basis`` marked PLACEHOLDER is not yet confirmed against the approvals and holds the
conservative reading. Update ``basis`` when you confirm an entry.

The flags are live. A change to ``training`` decides which scans can train a redistributed model.
"""
from __future__ import annotations

from clinicalomx import Policy

POLICIES: tuple[Policy, ...] = (
    Policy("projects", readable=True, ingest=True, training=True, derive=True,
           basis="internal cohorts"),
    Policy("external_projects", readable=True, ingest=True, training=False, derive=True,
           basis="PLACEHOLDER: collaborator scans; analysis permitted, model training NOT assumed"),
    # Catalogued for the integrity probe only. No derivatives, no training.
    Policy("cardiac_projects", readable=True, ingest=True, training=False, derive=False,
           basis="PLACEHOLDER: inspection and cataloguing permitted; derive/train not assumed"),
    # Internal QC and calibration scans, not study data. Probe only, never catalogue.
    Policy("qc_and_testing", readable=True, ingest=False, training=False, derive=False,
           basis="internal QC / calibration scans; not study data"),
    # The output root: every derivative this pipeline writes nests under `keys.PIPELINE_ROOT`.
    # `training` must stay False. Training gates read the source scan's folder, and granting it
    # here opens a route into training that bypasses the source agreements.
    Policy("clinicalomx-qptiff-pipeline", readable=True, ingest=True, training=False, derive=True,
           basis=("PLACEHOLDER: this pipeline's own derivative outputs, on the dbstorage root; "
                  "read/ingest/derive because the pipeline registers these keys, reads them back "
                  "and derives further from them; training withheld because the training gate "
                  "resolves the source scan's folder, so an output never needs it. Whether a "
                  "derivative inherits its source's agreement is unconfirmed against the approvals "
                  "- confirm before widening, and note the source folders' flags already bound "
                  "what may be produced here at all")),
)


def register() -> None:
    """Load :data:`POLICIES` into the framework. Idempotent.

    Call once before resolving any policy. Where this repo is not pip-installed (the HPC and the VM
    run a git checkout), the entry point is absent and this call is the only way the table loads.
    """
    from clinicalomx import governance

    governance.register_policies(POLICIES)


#: Flag name -> label, weakest first, in ``Policy.CHAIN`` order.
_FLAG_LABELS = (("readable", "read"), ("ingest", "ingest"), ("derive", "derive"),
                ("training", "train"))


def level(key_or_folder: str) -> str:
    """The highest flag a folder's policy grants: ``read``, ``ingest``, ``derive`` or ``train``.

    Takes an artifact key or a bare folder. Returns ``"denied"`` when nothing is granted, which
    includes an undeclared folder.
    """
    from clinicalomx import governance

    policy = governance.policy_for(key_or_folder)
    granted = [label for flag, label in _FLAG_LABELS if getattr(policy, flag)]
    return granted[-1] if granted else "denied"
