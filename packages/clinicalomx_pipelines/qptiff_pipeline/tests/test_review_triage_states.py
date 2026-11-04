"""The triage board: ``bgnorm_running`` is a lane, ``bgnorm_failed`` is not.

A lane drop runs ``set_state``, so no lane may be terminal. ``bgnorm_failed`` leaves only through
``gate.reopen``, which lands at ``bgnorm_pending``. Uses the dev fakes; skipped without streamlit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from qptiff_pipeline.domain import states
from qptiff_pipeline.review.dev import fakes

fakes.install_modules()  # before the app module is imported

from qptiff_pipeline.review import app

APP = str(Path(__file__).resolve().parents[1] / "src/qptiff_pipeline/review/dev/app.py")

LANE_STATES = frozenset(s for _h, s in app.TRIAGE_LANES)


@pytest.fixture(autouse=True)
def _fresh_fake_world():
    """Rebuild the fake records around every test: the store is a module global."""
    fakes.reset_data()
    fakes.patch_app(app)
    app._unit_states.clear()
    yield
    fakes.reset_data()
    app._unit_states.clear()


def _tmas_at(state: states.S) -> list[tuple[str, str, str]]:
    return [row for row in app._unit_states("core") if row[2] == state]


# The split itself.
def test_no_lane_is_a_terminal_state() -> None:
    assert not (LANE_STATES & states.TERMINAL_STATES)


def test_every_reopen_state_is_terminal() -> None:
    assert app.TRIAGE_REOPEN_STATES
    assert set(app.TRIAGE_REOPEN_STATES) <= states.TERMINAL_STATES


def test_a_state_is_a_lane_or_a_reopen_state_never_both() -> None:
    assert not (LANE_STATES & set(app.TRIAGE_REOPEN_STATES))


def test_the_stranded_states_are_surfaced() -> None:
    assert states.S.BGNORM_RUNNING in LANE_STATES
    assert states.S.BGNORM_FAILED in app.TRIAGE_REOPEN_STATES
    assert {states.S.BGNORM_RUNNING, states.S.BGNORM_FAILED} <= set(app.TRIAGE_STATES)


def test_the_original_four_lanes_are_unchanged() -> None:
    assert {
        states.S.CORE_APPROVED,
        states.S.BGNORM_QUEUED,
        states.S.HE_PENDING,
        states.S.BGNORM_PENDING,
    } <= LANE_STATES


def test_widening_the_lanes_with_a_terminal_state_is_refused_at_import() -> None:
    import types

    source = Path(app.__file__).read_text().replace(
        '("H&E / no bgnorm", states.S.HE_PENDING),',
        '("H&E / no bgnorm", states.S.HE_PENDING),\n    ("Failed", states.S.BGNORM_FAILED),',
        1,
    )
    module = types.ModuleType("_triage_lane_guard_probe")
    module.__file__ = app.__file__
    with pytest.raises(ValueError, match="triage lanes must be non-terminal"):
        # The guard runs at import, so the module is re-executed.
        exec(compile(source, app.__file__, "exec"), module.__dict__)  # noqa: S102


# The two routes back to the queue.
def test_reopen_lands_at_the_gate_pending_not_in_the_queue() -> None:
    gate_cfg = states.GATES["bgnorm"]
    assert gate_cfg.rejected == states.S.BGNORM_FAILED
    assert gate_cfg.pending == states.S.BGNORM_PENDING
    assert gate_cfg.pending != states.S.BGNORM_QUEUED

    uid, _name, _s = _tmas_at(states.S.BGNORM_FAILED)[0]
    result = app.gate.reopen("bgnorm", uid)
    assert result["state"] == states.S.BGNORM_PENDING

    app._unit_states.clear()
    assert uid in {u for u, _n, _st in _tmas_at(states.S.BGNORM_PENDING)}


def test_a_running_tma_is_an_ordinary_lane_card() -> None:
    running = _tmas_at(states.S.BGNORM_RUNNING)
    assert running, "the fake world must hold a bgnorm_running TMA"
    assert states.S.BGNORM_RUNNING not in states.TERMINAL_STATES


def test_only_bgnorm_pending_clears_markers_on_the_way_to_the_queue() -> None:
    src = Path(app.__file__).read_text()
    assert "if cur == states.S.BGNORM_PENDING and tgt == states.S.BGNORM_QUEUED:" in src


# What the reviewer sees.
def _triage_run() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["stage"] = "triage"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_the_failed_tma_is_visible_behind_a_confirmed_reopen() -> None:
    uid, name, _s = _tmas_at(states.S.BGNORM_FAILED)[0]
    at = _triage_run()

    shown = [m.value for m in at.markdown if name in m.value]
    assert shown, f"{name} is not rendered on the triage board"
    assert "bgnorm_failed" in shown[0]

    button = at.button(key=f"triage-reopen-{uid}")
    assert button.disabled, "reopen must be gated on an explicit confirmation"
    at.checkbox(key=f"triage-reopen-ok-{uid}").check().run()
    assert not at.button(key=f"triage-reopen-{uid}").disabled


def test_reopening_from_the_board_moves_the_tma_out_of_the_terminal_state() -> None:
    uid, _name, _s = _tmas_at(states.S.BGNORM_FAILED)[0]
    at = _triage_run()
    at.checkbox(key=f"triage-reopen-ok-{uid}").check().run()
    at.button(key=f"triage-reopen-{uid}").click().run()
    assert not at.exception, [e.value for e in at.exception]

    app._unit_states.clear()
    by_uid = {u: s for u, _n, s in app._unit_states("core")}
    assert by_uid[uid] == states.S.BGNORM_PENDING


def test_a_running_tma_gets_no_reopen_control() -> None:
    at = _triage_run()
    reopen_keys = {b.key for b in at.button if b.key and b.key.startswith("triage-reopen-")}
    failed = {u for u, _n, _s in _tmas_at(states.S.BGNORM_FAILED)}
    running = {u for u, _n, _s in _tmas_at(states.S.BGNORM_RUNNING)}
    assert reopen_keys == {f"triage-reopen-{u}" for u in failed}
    assert not (reopen_keys & {f"triage-reopen-{u}" for u in running})
