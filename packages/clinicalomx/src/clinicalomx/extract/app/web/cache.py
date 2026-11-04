"""Process-wide caches behind the Explorer, keyed by the plain values `ExplorerState` holds.

Snapshots, the shared Index, describes and surveys are cached per generation; the Refresh button
bumps the generation and clears everything. There is no TTL. Plans are cached by content token.
"""
from __future__ import annotations

import collections
import functools
import os
from datetime import UTC, datetime

from clinicalomx.extract import submit as hop
from clinicalomx.extract.app import demo
from clinicalomx.extract.availability import describe as _describe
from clinicalomx.extract.availability import inspect_stores
from clinicalomx.extract.corpus import survey as _survey
from clinicalomx.extract.index import Index
from clinicalomx.extract.layout import layout_of
from clinicalomx.snapshot import Snapshot

#: Per-ROI cache size: the largest TMA grid plus its anchors. The oldest ROI is dropped first.
_PER_ROI = 128


def allow_blocking_reads() -> None:
    """Allow blocking lamindb (Django) reads on the Reflex event loop."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"


@functools.lru_cache(maxsize=2)
def demo_snapshot():
    """The synthetic instance. Reaches no database."""
    demo.install_governance()
    return demo.build_snapshot()


@functools.lru_cache(maxsize=4)
def live_snapshot(generation: int):
    """One Snapshot per generation."""
    return Snapshot()


def snapshot(*, live: bool, generation: int):
    return live_snapshot(generation) if live else demo_snapshot()


@functools.lru_cache(maxsize=4)
def index(*, live: bool, generation: int):
    """One shared Index per generation, read by every view on the page."""
    return Index.of(snapshot(live=live, generation=generation))


def roi_options(*, live: bool, generation: int) -> list:
    """Every ROI the sidebar may offer, in the order it offers them."""
    snap = snapshot(live=live, generation=generation)
    if not live:
        return list(demo.rois(snap))
    return [r for t in snap.roi_types() for r in snap.records(t)]


@functools.lru_cache(maxsize=_PER_ROI)
def describe(*, live: bool, generation: int, roi_uid: str):
    """What one ROI can give you. Never probes storage: the control plane cannot reach it."""
    snap = snapshot(live=live, generation=generation)
    roi = _record(snap, roi_uid)
    if roi is None:
        return None
    return _describe(roi, index=index(live=live, generation=generation))


def _record(snap, roi_uid: str):
    """The ROI record behind a uid, from the Snapshot when it holds one."""
    if not roi_uid:
        return None
    return snap.record_by_uid(roi_uid)


@functools.lru_cache(maxsize=16)
def survey(*, live: bool, generation: int, group_by: str):
    """What exists across the instance. One survey per grouping per generation."""
    return _survey(index=index(live=live, generation=generation),
                   group_by=group_by or None)


@functools.lru_cache(maxsize=_PER_ROI)
def inspect(*, live: bool, generation: int, roi_uid: str):
    """What is inside the stores this ROI's elements address. Opens files, so it is opt-in."""
    av = describe(live=live, generation=generation, roi_uid=roi_uid)
    return inspect_stores(av) if av is not None else {}


# Plans, and the layout of what they would build.
#: Bounded plan store, oldest evicted.
_PLANS: collections.OrderedDict[str, object] = collections.OrderedDict()
_PLANS_MAX = 32


def remember_plan(plan) -> str:
    """Keep a plan where an event handler can find it again, and return its token."""
    token = plan.token
    _PLANS.pop(token, None)
    _PLANS[token] = plan
    while len(_PLANS) > _PLANS_MAX:
        _PLANS.popitem(last=False)
    return token


def plan(token: str):
    return _PLANS.get(token)


@functools.lru_cache(maxsize=32)
def layout(*, token: str, live: bool, generation: int, measure: bool):
    """The shape of the object a plan would produce, keyed by token and ``measure``."""
    result = plan(token)
    if result is None:
        return None
    return layout_of(result, snapshot=snapshot(live=live, generation=generation), measure=measure)


@functools.lru_cache(maxsize=4)
def submitted_runs(jobs_generation: int):
    """Submitted runs and when they were read. Has its own generation, separate from the page's."""

    return list(hop.runs()), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_CACHES = (demo_snapshot, live_snapshot, index, describe, survey, inspect, layout,
           submitted_runs)


def clear_all() -> None:
    """Clear every cache."""
    for cache in _CACHES:
        cache.cache_clear()
    from clinicalomx.extract.app.web import dot

    dot._CACHE.clear()
