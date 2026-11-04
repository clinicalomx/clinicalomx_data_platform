"""The curation entry point against the ledger: the session opens a run, and what it writes
cites it. The offline half of the same path is `tests/test_curate_session.py`."""

from __future__ import annotations

import lamindb as ln
from clinicalomx.review import ensure_state_feature
from clinicalomx_ledger import reads
from clinicalomx_ledger.grammar.store import LedgerClaimStore
from tests.conftest import fixture_transport

from clinicalomx_markers.curate import session
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from conftest import BRANCH, HUMAN, subject


def test_a_session_opens_a_run_and_everything_it_writes_cites_it(tmp_path):
    # Declare the state feature on main, before a run makes the run's branch the default.
    ensure_state_feature(feature=MARKER_DOMAIN.state_feature)
    marker = subject("NAKATPASE")
    store = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **HUMAN)

    try:
        run, refused = session.curate_all(
            ["NakATPase"], store=store, branch=BRANCH, snapshots=tmp_path, by="rafael",
            ask=lambda prompt: "", today="2026-09-25", fetch=fixture_transport(),
            say=lambda line: None)
    finally:
        # Restore the default branch that `ln.finish` changes.
        ln.context._run = None
        ln.context._branch = None

    assert run is not None and refused == []
    (claim,) = store.held(marker, predicate=MarkerPredicate.SEGMENTATION_ROLE)
    assert store.lineage(claim).run_uid == run.uid
    cited = reads.by_run(run.uid)
    assert {c.subject for c in cited} == {marker}
    assert len(cited) == 2, "the claim and the decision accepting it, both inside the run"
    (answer,) = store.answers(marker, predicate=MarkerPredicate.SEGMENTATION_ROLE)
    assert answer.value == claim.value
    # Membership, not equality: the ledger is shared across tests.
    assert marker in store.in_state("accepted")
