"""Dev entrypoint: the real review app UI, on fake data, with live reload.

    ./dev_review_app.sh              # http://localhost:8599

Installs the fakes before the app imports lamindb, and reloads any edited project module on each
rerun. Every lamindb and Prefect read comes from ``dev/fixtures.py``; every write goes to an
in-memory store in ``dev/fakes.py``.
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import streamlit as st

from qptiff_pipeline.review.dev import fakes, fixtures

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("review.dev")

fakes.install_modules()  # before the app module is imported

from qptiff_pipeline.review import app as review_app


def _tracked() -> list[tuple[str, object, Path]]:
    """(name, module, path) for every imported project module, found in ``sys.modules``."""
    out = []
    for name, mod in list(sys.modules.items()):
        if not name.startswith("qptiff_pipeline") or name.endswith(".dev.app"):
            continue  # this file; Streamlit re-executes it
        path = getattr(mod, "__file__", None)
        if path and path.endswith(".py") and Path(path).is_file():
            out.append((name, mod, Path(path)))
    return out


@st.cache_resource(show_spinner=False)
def _reload_stamps() -> dict[str, int]:
    """Mtime of each module as last imported, by name. One dict per process: modules are global."""
    return {}


def _reload_if_edited() -> bool:
    """Reload every project module whose file changed since it was imported.

    The app module reloads last, so it re-binds the names it imported from the others.
    """
    tracked = _tracked()
    stamps = _reload_stamps()
    fresh = not stamps  # first run in this process; modules were just imported
    changed = []
    for name, mod, path in tracked:
        mtime = path.stat().st_mtime_ns
        if not fresh and stamps.get(name) not in (None, mtime):
            changed.append((name, mod))
        stamps[name] = mtime
    if not changed:
        return False
    for name, mod in changed:
        if mod is not review_app:
            importlib.reload(mod)
            log.info("dev: reloaded %s", name)
    importlib.reload(review_app)  # last: re-binds what it imported by name
    st.cache_data.clear()  # cached renders came from the old code
    fakes.install_modules()
    return True


def _dev_sidebar(reloaded: bool) -> None:
    with st.sidebar:
        st.caption("🧪 **DEV MODE**: fake lamindb/Prefect, in-memory writes")
        st.caption(f"{len(fixtures.RECORDS)} record(s) · {len(fixtures.BUNDLES)} bundle(s)"
                   + ("  ·  ♻️ reloaded" if reloaded else ""))
        st.divider()
        if st.button("Reset fake data", width="stretch"):
            fakes.reset_data()
            st.cache_data.clear()
            st.session_state.clear()
            st.rerun()
        if st.button("Clear caches", width="stretch"):
            st.cache_data.clear()
            st.rerun()


reloaded = _reload_if_edited()
fakes.patch_app(review_app)  # re-point at the fakes; must follow every reload
_dev_sidebar(reloaded)
review_app.main()
