"""Invariants that depend on this repo's stage semantics: its gates (``S``), key layout and outputs.

:func:`pipeline_checks` is what ``pyproject.toml`` advertises under ``clinicalomx.checks``.

  - a ``core_approved`` TMA with no Cores  -> core-persist wrote bytes and died before registering
  - a ``bgnorm_pending`` TMA with no bgnormed core -> the runner advanced on nothing
  - QC rows != channel count -> a channel dropped out of the GMM fit
  - registered qptiffs != qptiffs on disk -> files the integrity gate cannot see
  - accepted parquet missing its contracted columns -> a broken cross-stage schema
"""
from __future__ import annotations

import os

from clinicalomx.checks import Check, Snapshot

# The accepted core grid's columns. core-persist writes them, bgnorm and core-detect read them.
# REQUIRED columns are always written. `roi_id` is missing from older parquets, which fall back to
# position.
CORES_PARQUET_REQUIRED = ("row", "col", "class", "is_detected", "geometry")
CORES_PARQUET_ADOPTED = ("roi_id",)

# Reports unregistered qptiffs as per-directory counts. The root is storage.raw_root in site.yaml,
# which the HPC worker exports into every job.
SOURCE_ROOT_ENV = "CLINICALOMX_SITE_RAW_ROOT"

def _channel_columns() -> tuple[str, ...]:
    """The channel columns the two QptiffChannels checks read. Add a name when a check reads it."""
    from clinicalomx.names import FEATURES

    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    return (FEATURES.SOURCE_ARTIFACT, qptiff_vocab.QPTIFF_CH_INDEX)


def approved_tmas_have_cores(snap: Snapshot) -> list[str]:
    """A TMA past the core gate must have registered Cores.

    ``core_approved`` is set at the end of core-persist, so a TMA without Cores means persist died
    after the parquet write.
    """
    from qptiff_pipeline.domain import states

    past_core = {states.S.CORE_APPROVED, states.S.BGNORM_QUEUED, states.S.BGNORM_RUNNING,
                 states.S.BGNORM_PENDING, states.S.COMPLETE}
    cores_by_parent: dict[str, int] = {}
    for core in snap.records(states.CORE_TYPE):
        parent = core.name.rsplit(":", 1)[0]
        cores_by_parent[parent] = cores_by_parent.get(parent, 0) + 1

    out = []
    tma_states = snap.states(states.TMA_TYPE)
    for tma in snap.records(states.TMA_TYPE):
        if tma_states.get(tma.uid) in past_core and not cores_by_parent.get(tma.name):
            out.append(f"{tma.name} is {tma_states.get(tma.uid)} with 0 Cores")
    return out


def bgnorm_pending_tmas_have_results(snap: Snapshot) -> list[str]:
    """A TMA the runner advanced to bgnorm_pending must have at least one bgnormed core.

    The runner advances on a partial result, so the check is ">= 1", not "all".
    """
    from qptiff_pipeline.domain import states, vocab

    # Presence, not resolution: resolving each Core's artifact is one query per value.
    done_by_parent: dict[str, int] = {}
    for core in snap.records(states.CORE_TYPE):
        if snap.values(core).get(vocab.BGNORMED) is not None:
            parent = core.name.rsplit(":", 1)[0]
            done_by_parent[parent] = done_by_parent.get(parent, 0) + 1

    out = []
    tma_states = snap.states(states.TMA_TYPE)
    for tma in snap.records(states.TMA_TYPE):
        if tma_states.get(tma.uid) == states.S.BGNORM_PENDING and not done_by_parent.get(tma.name):
            out.append(f"{tma.name} is bgnorm_pending with 0 bgnormed cores")
    return out


def bgnormed_cores_have_qc(snap: Snapshot) -> list[str]:
    """``bgnormed`` is written after the QC parquet, so a zarr without QC is a fault."""
    from qptiff_pipeline.domain import states, vocab

    out = []
    for core in snap.records(states.CORE_TYPE):
        vals = snap.values(core)
        has_zarr = vals.get(vocab.BGNORMED) is not None
        has_qc = vals.get(vocab.BGNORM_QC_FEATURE) is not None
        if has_zarr and not has_qc:
            out.append(f"{core.name} has a bgnormed zarr but no QC table")
    return out


def cores_have_geometry_identity(snap: Snapshot) -> list[str]:
    """Every Core needs a way to find its geometry row: ``roi_id`` or ``parquet_row``.

    ``roi_id`` is preferred. ``parquet_row`` is the older fallback.
    """
    from qptiff_pipeline.domain import states

    out = []
    for core in snap.records(states.CORE_TYPE):
        vals = snap.values(core)
        if vals.get("roi_id") is None and vals.get("parquet_row") is None:
            out.append(f"{core.name} has neither roi_id nor parquet_row")
    return out


def qptiffs_on_disk_are_registered(snap: Snapshot) -> list[str]:
    """Source freshness: qptiffs on disk but missing from the registry, by top-level directory.

    One line per directory, with a count and a few examples. Hidden and trash paths are skipped.
    Needs the filesystem, so it runs on the HPC only.
    """
    from pathlib import Path

    from qptiff_pipeline.lamin.queries import source_artifacts

    raw = os.environ.get(SOURCE_ROOT_ENV, "")
    root = Path(raw)
    if not raw or not root.exists():
        return [f"{SOURCE_ROOT_ENV} is unset or not mounted (run this on the HPC)"]

    on_disk: set[str] = set()
    for p in root.rglob("*.qptiff"):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .Trash-*/ and friends
        on_disk.add(str(rel))

    from clinicalomx import governance

    missing = sorted(on_disk - {a.key for a in source_artifacts(".qptiff")})
    by_dir: dict[str, list[str]] = {}
    for key in missing:
        by_dir.setdefault(governance.folder_of(key), []).append(key)

    # Only a folder whose policy permits ingest can have a freshness gap.
    out = []
    for folder, keys_ in sorted(by_dir.items(), key=lambda kv: -len(kv[1])):
        if not governance.permits_ingest(f"{folder}/x"):
            continue
        out.append(f"{folder}/: {len(keys_)} unregistered (e.g. {Path(keys_[0]).name})")
    return out


def every_source_folder_has_a_policy(snap: Snapshot) -> list[str]:
    """Every folder of scans under the source root must have a declared governance policy.

    An undeclared folder is denied by default. This check makes it visible so someone decides.
    """
    from pathlib import Path

    from clinicalomx import governance

    raw = os.environ.get(SOURCE_ROOT_ENV, "")
    root = Path(raw)
    if not raw or not root.exists():
        return [f"{SOURCE_ROOT_ENV} is unset or not mounted (run this on the HPC)"]

    declared = set(governance.declared_folders())
    found = {
        rel.parts[0]
        for p in root.rglob("*.qptiff")
        for rel in [p.relative_to(root)]
        if len(rel.parts) > 1 and not any(part.startswith(".") for part in rel.parts)
    }
    return [f"{f}/ holds scans but has no declared policy (denied by default)"
            for f in sorted(found - declared)]


def accepted_parquets_match_the_contract(snap: Snapshot) -> list[str]:
    """The accepted core grid must carry its contracted columns. Reads bytes, so the HPC only."""
    import geopandas as gpd
    from clinicalomx import newest_artifact

    from qptiff_pipeline.domain import states, vocab

    out = []
    for tma in snap.records(states.TMA_TYPE):
        art = newest_artifact(snap.values(tma).get(vocab.F_CORES_ACCEPTED))
        if art is None:
            continue
        try:
            cols = set(gpd.read_parquet(art.path).columns)
        except Exception as exc:  # noqa: BLE001 - an unreadable parquet IS the finding
            out.append(f"{tma.name}: cannot read {art.key} ({type(exc).__name__})")
            continue
        missing = [c for c in CORES_PARQUET_REQUIRED if c not in cols]
        if missing:
            out.append(f"{tma.name}: {art.key.split('/')[-1]} lacks {missing}")
    return out


def accepted_parquets_have_adopted_roi_id(snap: Snapshot) -> list[str]:
    """How many accepted parquets carry ``roi_id``. Informational, not a fault.

    Older parquets resolve by ``parquet_row``. The count falls to zero as TMAs are re-accepted.
    """
    import geopandas as gpd
    from clinicalomx import newest_artifact

    from qptiff_pipeline.domain import states, vocab

    out = []
    for tma in snap.records(states.TMA_TYPE):
        art = newest_artifact(snap.values(tma).get(vocab.F_CORES_ACCEPTED))
        if art is None:
            continue
        try:
            cols = set(gpd.read_parquet(art.path).columns)
        except Exception:  # noqa: BLE001, S112 - accepted_parquets_match_the_contract already
            continue        # reports an unreadable parquet; duplicating it here would double-count
        if any(c not in cols for c in CORES_PARQUET_ADOPTED):
            out.append(f"{tma.name}: predates roi_id (resolves positionally)")
    return out


def qptiff_channels_name_their_source(snap: Snapshot) -> list[str]:
    """Every ``QptiffChannels`` Record carries the ``source_artifact`` it was read from.

    The type is outside the ROI umbrella, so the framework's anchoring invariants do not cover it.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab

    # Required before iterating: without it, `values()` runs one query per record. Load named
    # columns only.
    snap.load_values(qptiff_vocab.QPTIFF_CHANNELS_TYPE, _channel_columns())
    return [
        f"{record.name} has no source_artifact"
        for record in snap.records(qptiff_vocab.QPTIFF_CHANNELS_TYPE)
        if snap.values(record).get("source_artifact") is None
    ]


def qptiff_channel_indices_are_contiguous(snap: Snapshot) -> list[str]:
    """One qptiff's channel Records cover ``0..N-1`` exactly once.

    Catches a channel table that check-qptiffs left half written. Checks the index set, not a count
    against ``qptiff_n_channels``. Files with no channel Records are skipped.
    """
    from collections import defaultdict

    from qptiff_pipeline.domain import qptiff as qptiff_vocab


    snap.load_values(qptiff_vocab.QPTIFF_CHANNELS_TYPE, _channel_columns())  # see the check above
    by_source: dict[str, list[int]] = defaultdict(list)
    for record in snap.records(qptiff_vocab.QPTIFF_CHANNELS_TYPE):
        values = snap.values(record)
        source = values.get("source_artifact")
        index = values.get(qptiff_vocab.QPTIFF_CH_INDEX)
        if source is None or index is None:
            continue  # reported by qptiff_channels_name_their_source; not double-counted here
        by_source[str(source)].append(int(index))

    out = []
    for source, indices in sorted(by_source.items()):
        expected = set(range(len(indices)))
        if sorted(indices) != sorted(expected):
            duplicated = sorted({i for i in indices if indices.count(i) > 1})
            missing = sorted(expected - set(indices))
            detail = ", ".join(
                part for part in (
                    f"missing {missing}" if missing else "",
                    f"duplicated {duplicated}" if duplicated else "",
                ) if part
            ) or f"indices {sorted(indices)}"
            out.append(f"{source}: {len(indices)} channel Record(s), {detail}")
    return out


def pipeline_checks() -> list[Check]:
    """This repo's own invariants."""
    return [
        Check("pipeline.approved_tmas_have_cores", "error", approved_tmas_have_cores,
              "a TMA past the core gate has registered Cores"),
        Check("pipeline.bgnorm_pending_tmas_have_results", "error",
              bgnorm_pending_tmas_have_results,
              "the runner only advances a TMA with >= 1 bgnormed core"),
        Check("pipeline.bgnormed_cores_have_qc", "error", bgnormed_cores_have_qc,
              "bgnormed is written after the QC table, so a zarr implies QC"),
        Check("pipeline.cores_have_geometry_identity", "error", cores_have_geometry_identity,
              "a Core can locate its geometry row (roi_id, or the parquet_row fallback)"),
        Check("pipeline.qptiff_channels_name_their_source", "error",
              qptiff_channels_name_their_source,
              "every QptiffChannels Record names the qptiff it was read from"),
        Check("pipeline.qptiff_channel_indices_are_contiguous", "error",
              qptiff_channel_indices_are_contiguous,
              "one qptiff's channel Records cover 0..N-1 exactly once"),
        # Storage-backed: skipped unless the run has byte access.
        Check("pipeline.qptiffs_on_disk_are_registered", "warn",
              qptiffs_on_disk_are_registered,
              "source freshness, for folders whose policy permits ingest",
              needs_storage=True),
        Check("pipeline.every_source_folder_has_a_policy", "warn",
              every_source_folder_has_a_policy,
              "a folder of scans with no declared governance policy is denied, but should be decided",
              needs_storage=True),
        Check("pipeline.accepted_parquets_match_the_contract", "error",
              accepted_parquets_match_the_contract,
              f"the accepted core grid carries {list(CORES_PARQUET_REQUIRED)}",
              needs_storage=True),
        Check("pipeline.accepted_parquets_have_adopted_roi_id", "warn",
              accepted_parquets_have_adopted_roi_id,
              "roi_id adoption progress; pre-change parquets resolve positionally",
              needs_storage=True),
    ]

