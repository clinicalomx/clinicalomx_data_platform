"""The HITL review contract: what each detect stage publishes and the review app reads.

The detect flows (on the HPC) and the review app (on the VM) must agree on two things:

* the Prefect artifact keys a payload travels under. The VM cannot reach HPC storage, so a review
  image travels as a base64 data-URI in a keyed Prefect artifact. The newest version is current.
* the editable classes per stage, which decide what a reviewer may relabel.

Per unit, each detect flow emits a review overlay under ``<review prefix><slug>`` and an editor
bundle under ``<edit prefix><slug>`` (JSON: clean image b64 + boxes). Nothing here is written to
the instance.
"""
from __future__ import annotations

#: Classes the general stage detects on a WholeSlide.
CLASSES: tuple[str, ...] = ("TMA", "tissue_section")

#: What a reviewer may relabel, per stage. general reviews a WholeSlide's TMA and tissue_section
#: boxes. core reviews each TMA's cores.
CLASSES_BY_STAGE: dict[str, tuple[str, ...]] = {
    "general": CLASSES,
    # inferred_core: a cell the dearray grid fit filled where the detector found nothing. A
    # reviewer can promote it to full_core or partial_core.
    "core": ("full_core", "partial_core", "inferred_core"),
}

EDIT_ARTIFACT_PREFIX = "roi-edit-"
EDIT_PREFIX_BY_STAGE: dict[str, str] = {"general": EDIT_ARTIFACT_PREFIX, "core": "core-edit-"}
REVIEW_PREFIX_BY_STAGE: dict[str, str] = {"general": "roi-review-", "core": "core-review-"}

#: Markdown-artifact prefix carrying the qptiff's macro thumbnail (base64 PNG) to the VM. It shows
#: the modality: H&E is pink and white, multiplex is dark and multicoloured.
QPTIFF_THUMB_PREFIX = "qptiff-thumb-"

#: Table-artifact prefix (+ core-name slug) for per-Core bgnorm QC metrics. The ``bgnorm_qc``
#: Artifact is the durable copy.
BGNORM_QC_PREFIX = "bgnorm-qc-"

#: Table-artifact key for the bgnorm-runner's live manifest: which bgnorm-core run holds which TMA's
#: cores. One key, rewritten each poll.
BGNORM_MANIFEST_KEY = "bgnorm-runner-manifest"
