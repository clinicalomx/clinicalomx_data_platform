"""A curation session: open its run, then curate segmentation roles inside it.

    uv run python -m clinicalomx_markers.curate.session --by rafael NakATPase Ecad
    uv run python -m clinicalomx_markers.curate.session --by rafael --branch curation NakATPase
    uv run python -m clinicalomx_markers.curate.session --by rafael --release marker-rel-x.json Ecad

Each name is resolved, its seeded UniProt accession is snapshotted, a role is proposed, and the
human answers on stdin: an empty line accepts, `later` leaves the claim in the review queue,
and anything else rejects it and is kept as the reason. Offline by default, into
`MemoryClaimStore`, with no run. With ``--branch`` it writes to the ledger inside a run opened
by `track`.

Names resolve against a release export: the file ``--release`` names, or the newest
``marker-*.json`` in ``--exports`` (the current directory by default). With no export, only
canonical names resolve.

`import lamindb` is deferred into functions: importing it connects to the instance.

`record_role` is the write step a notebook calls; the store is an argument. Only the store
moves a subject into the review queue. A rejection of a claim already rejected writes nothing.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import DecisionKind, Outcome, in_effect, short_id
from clinicalomx_ledger.grammar.published import Published, load
from clinicalomx_ledger.grammar.store import ClaimStore, LedgerClaimStore, MemoryClaimStore

from ..domain.marker import MARKER_DOMAIN, MarkerClaim, MarkerPredicate, MarkerSource
from ..domain.target import TargetSet, seed_claims
from ..propose.localisation import record_from
from ..propose.role import RuleBasedRoleProposer, as_claim
from ..propose.schema import ProposalRequest, Proposer
from ..resolve import Kind, resolve
from ..sources.snapshot import Snapshot, Transport, capture, http
from ..sources.uniprot import UniProtRecord, url_for

PROMPT = "empty line accepts, 'later' leaves it for review, anything else rejects as the reason: "

#: The answer that proposes without deciding, so the claim waits in the review queue.
LATER = "later"

#: The one Transform key for every curation step.
CURATION_KEY = "clinicalomx_markers/curate"


def accession(marker: str) -> str:
    """The one UniProt accession ``marker``'s seeded target names, or "" when there is none.

    A target that is a candidate set, or a stain with no target, gives "".
    """
    for claim in seed_claims():
        if claim.subject == marker:
            target = TargetSet.parse(claim.value)
            return target.candidates[0] if len(target.candidates) == 1 else ""
    return ""


def newest_export(exports: Path) -> Published | None:
    """The newest ``marker-*.json`` export in ``exports``, or None. Other domains are skipped."""
    found = [p for p in map(load, sorted(exports.glob("marker-*.json")))
             if p.domain == MARKER_DOMAIN.name]
    return max(found, key=lambda p: datetime.fromisoformat(p.released_at), default=None)


def unresolved(raw: str, release: Published | None) -> str:
    """Why ``raw`` did not resolve, naming the release consulted or its absence."""
    if release is None:
        return (f"{raw}: unresolved - no release export found, so no accepted alias could be "
                f"read (cut and export a release, or pass --release)")
    return f"{raw}: unresolved against release {release.release_uid}, nothing guessed"


def subject_row(marker: str, record: UniProtRecord) -> dict[str, str]:
    """The row the rules match. `compartment` is what the mitochondrial veto reads."""
    return {"canonical_name": marker, "compartment": " ".join(record.locations)}


def record_role(
    store: ClaimStore,
    snapshot: Snapshot,
    record: UniProtRecord,
    *,
    marker: str,
    role: str,
    judgement: Judgement | None = None,
    outcome: Outcome | None = Outcome.ACCEPTED,
) -> MarkerClaim:
    """Write one reviewed segmentation-role claim. The store moves its subject and records the
    decision.

    ``judgement`` is required whenever ``outcome`` decides something. ``outcome`` None only
    proposes: the claim is written with no decision, and any judgement is ignored. The subject
    written is the marker.
    """
    if judgement is None and outcome is not None:
        raise ValueError(f"{marker}: {str(outcome)!r} decides something, so it needs "
                         "a judgement saying who decided it and when")
    claim = MarkerClaim(
        subject=marker,
        predicate=MarkerPredicate.SEGMENTATION_ROLE,
        value=role,
        source=snapshot.source,
        source_id=snapshot.source_id,
        source_version=record.release,
        evidence=record.evidence,
    )
    return store.add(claim, outcome=outcome, judgement=judgement,
                     subject_row=subject_row(marker, record))


def curate(
    raw: str,
    *,
    store: ClaimStore,
    snapshots: Path,
    by: str,
    ask: Callable[[str], str],
    today: str,
    release: Published | None = None,
    fetch: Transport = http,
    proposer: Proposer | None = None,
    say: Callable[[str], None] = print,
) -> MarkerClaim | None:
    """Resolve one name, snapshot its source, propose a role, ask the human, write the answer.

    Returns the stored claim, or None when nothing was written. A refused write raises.
    """
    got = resolve(raw, release, kind=Kind.MARKER)
    if not got.resolved:
        say(unresolved(raw, release))
        return None
    marker = got.canonical

    found = accession(marker)
    if not found:
        say(f"{marker}: no single UniProt accession seeded, nothing to fetch")
        return None

    snapshot = capture(snapshots, source=MarkerSource.UNIPROT, source_id=found,
                       retrieved_at=today, fetch=lambda: fetch(url_for(found)))
    record = record_from(snapshot)
    request = ProposalRequest(marker=marker, evidence=record.evidence,
                              locations=record.locations, go_cc=record.go_cc)
    proposal = (proposer or RuleBasedRoleProposer()).propose(request)[0]
    if not proposal.usable:
        say(f"{marker}: the proposer refused - {proposal.reasoning}")
        return None
    claim = as_claim(proposal, request, source=snapshot.source, source_id=snapshot.source_id,
                     evidence=record.evidence, source_version=record.release)

    say(f"{marker} {proposal.predicate} = {proposal.value!r} "
        f"(confidence {proposal.confidence:.2f}, {proposal.triage.label.value}): "
        f"{proposal.reasoning}")
    answer = ask(PROMPT).strip()

    if answer == LATER:
        outcome, judgement = None, None
    elif not answer:
        outcome, judgement = Outcome.ACCEPTED, Judgement(by=by, at=today)
    else:
        outcome = Outcome.REJECTED
        judgement = Judgement(by=by, at=today, kind=DecisionKind.ONE_OFF, reason=answer)

    if outcome == Outcome.REJECTED:
        ruling = in_effect(store.decisions(marker, predicate=claim.predicate)).get(
            short_id(claim.claim_id()))
        if ruling is not None and ruling.outcome == Outcome.REJECTED:
            say(f"{marker}: already rejected by {ruling.by} on {ruling.at}, nothing written")
            return claim

    written = store.add(claim, outcome=outcome, judgement=judgement,
                        subject_row=subject_row(marker, record))
    say(f"{marker}: {outcome or 'queued for review'}")
    return written


def curate_all(
    raws: Sequence[str],
    *,
    store: ClaimStore,
    branch: str,
    snapshots: Path,
    by: str,
    ask: Callable[[str], str],
    today: str,
    release: Published | None = None,
    fetch: Transport = http,
    proposer: Proposer | None = None,
    say: Callable[[str], None] = print,
):
    """Curate every name inside one run, and return ``(run, refused)``.

    ``branch`` "" means no instance and no run. A refused write is reported and the session
    continues; ``refused`` names every one.
    """
    refused: list[str] = []
    with opened(branch, {"markers": list(raws)}) as run:
        if run is not None:
            say(f"run {run.uid} on branch {branch}")
        for raw in raws:
            try:
                curate(raw, store=store, snapshots=snapshots, by=by, ask=ask, today=today,
                       release=release, fetch=fetch, proposer=proposer, say=say)
            except ValueError as refusal:
                say(f"{raw}: REFUSED, nothing written - {refusal}")
                refused.append(raw)
    return run, refused


def track(*, branch: str, params: dict | None = None, key: str = CURATION_KEY):
    """Open the run this session's claims are attributed to, set `ln.context.run`, and return it.

    ``branch`` has no default: curation must never land on ``main`` by omission. Pair with
    `ln.finish()` at the end of the session.
    """
    import lamindb as ln

    transform = (
        ln.Transform.filter(key=key, is_latest=True).first()
        or ln.Transform(key=key, type="pipeline").save()
    )
    ln.track(transform=transform, branch=branch, params=params or {})
    return current()


def current():
    """The run in progress, or None when nothing called `track`."""
    import lamindb as ln

    return ln.context.run


@contextmanager
def opened(branch: str, params: dict) -> Iterator:
    """The run a curation session writes inside, finished whatever happens.

    ``branch`` "" opens none and yields None.
    """
    run = track(branch=branch, params=params) if branch else None
    try:
        yield run
    finally:
        if run is not None:
            import lamindb as ln

            ln.finish()


def main(argv: Sequence[str], *, ask: Callable[[str], str] = input,
         fetch: Transport = http) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m clinicalomx_markers.curate.session",
        description="Curate segmentation roles for a panel, inside a lamindb run.")
    parser.add_argument("markers", nargs="+", help="channel names as the vendor wrote them")
    parser.add_argument("--by", required=True,
                        help="the human deciding; no default, a decision names who made it")
    parser.add_argument("--branch", default="",
                        help="write to the ledger on this branch and open a run; "
                             "omitted, claims go to memory and no run is opened")
    parser.add_argument("--snapshots", type=Path,
                        help="where UniProt snapshots are kept; a temp dir when omitted")
    parser.add_argument("--release", type=Path,
                        help="the release export to resolve aliases from; "
                             "omitted, the newest marker export in --exports")
    parser.add_argument("--exports", type=Path, default=Path("."),
                        help="where marker-*.json exports are found; the current directory")
    args = parser.parse_args(argv)

    release = load(args.release) if args.release else newest_export(args.exports)
    if release is not None and release.domain != MARKER_DOMAIN.name:
        parser.error(f"{args.release} is a {release.domain!r} release, not a marker one")
    if release is not None:
        print(f"resolving against release {release.release_uid}")

    if args.branch:
        store = LedgerClaimStore(branch=args.branch, domain=MARKER_DOMAIN,
                                 actor=args.by, actor_kind="human")
    else:
        store = MemoryClaimStore(MARKER_DOMAIN)
    snapshots = args.snapshots or Path(tempfile.mkdtemp()) / "snapshots"
    today = datetime.now(UTC).date().isoformat()

    _, refused = curate_all(args.markers, store=store, branch=args.branch,
                            snapshots=snapshots, by=args.by, ask=ask, today=today,
                            release=release, fetch=fetch)

    for raw in args.markers:
        got = resolve(raw, release, kind=Kind.MARKER)
        if got.resolved:
            roles = [d.value for d in store.answers(
                got.canonical, predicate=MarkerPredicate.SEGMENTATION_ROLE)]
            print(f"{got.canonical}: accepted {roles or 'nothing'}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
