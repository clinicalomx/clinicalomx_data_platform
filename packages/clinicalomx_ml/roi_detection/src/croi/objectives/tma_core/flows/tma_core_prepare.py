"""The ``ingest-labels-core`` and ``split-core`` deployments: the two tma_core data stages.

Each runs the stage's ``run`` and logs a ``STATE: sentence`` verdict comparing the instance before
and after. Over an unchanged instance the verdict is ``UNCHANGED``. Imports other than prefect
stay in the flow bodies.
"""
from __future__ import annotations

from prefect import flow

LABELS_CONFIG = "1_ingest_labels.yaml"
SPLIT_CONFIG = "2_split.yaml"
SPLITS = ("train", "val", "test")


def _latest(key: str) -> tuple[str, int] | None:
    """The latest Collection at ``key`` as ``(uid, member count)``, or None."""
    import lamindb as ln

    coll = ln.Collection.filter(key=key, is_latest=True).one_or_none()
    return None if coll is None else (coll.uid, coll.artifacts.count())


def _compare(key: str, before, after) -> tuple[str, bool]:
    """One line for one Collection, and whether the stage wrote a new version."""
    uid, n = after
    if before is None:
        return f"{key}: CREATED uid={uid} with {n} artifacts", True
    if before[0] != uid:
        return f"{key}: NEW VERSION uid={uid} with {n} artifacts (was uid={before[0]})", True
    return f"{key}: unchanged uid={uid} with {n} artifacts", False


@flow(name="core-ingest-labels", log_prints=True)
def core_ingest_labels(config: str = LABELS_CONFIG) -> str:
    """Register the core labels and bundle ``clinical-roi/core``. Returns the verdict line.

    config: stage yaml name or path.
    """
    from croi.objectives.tma_core import environment
    from croi.objectives.tma_core.config import IngestLabelsConfig

    cfg = environment.load_config(config, IngestLabelsConfig)
    import lamindb as ln

    from croi.objectives.tma_core import labels

    environment.preflight(roi_schema=False)  # connect before reading "before"
    key = cfg["outputs"]["dataset"]["key"]
    prefix = cfg["outputs"]["labels"]["key_template"].split("{", 1)[0]
    labels_before, coll_before = ln.Artifact.filter(key__startswith=prefix).count(), _latest(key)

    labels.run(cfg)

    new_labels = ln.Artifact.filter(key__startswith=prefix).count() - labels_before
    line, wrote = _compare(key, coll_before, _latest(key))
    print(f"\n{line}")
    print(f"{prefix}: {new_labels} new artifacts")
    if wrote or new_labels:
        return f"WROTE: {new_labels} new label artifacts, {line}. Next: split."
    return "UNCHANGED: no new label artifact and no new collection version. Next: split."


@flow(name="core-split", log_prints=True)
def core_split(config: str = SPLIT_CONFIG) -> str:
    """Write the slide-stratified train/val/test Collections. Returns the verdict line.

    config: stage yaml name or path.
    """
    from croi.objectives.tma_core import environment
    from croi.objectives.tma_core.config import SplitConfig

    cfg = environment.load_config(config, SplitConfig)
    from croi.objectives.tma_core import split

    environment.preflight(roi_schema=False)  # connect before reading "before"
    keys = [f"{cfg['outputs']['splits']['key_prefix']}/{name}" for name in SPLITS]
    before = {k: _latest(k) for k in keys}

    split.run(cfg)

    lines = [_compare(k, before[k], _latest(k)) for k in keys]
    print()
    for line, _ in lines:
        print(line)
    wrote = [k.rsplit("/", 1)[1] for k, (_, w) in zip(keys, lines) if w]
    if wrote:
        return f"WROTE: a new version of {', '.join(wrote)}. Next: train."
    return "UNCHANGED: no new split version. Next: train."
