"""The curation API, version 1: routes under ``/api/v1``, JSON in and out.

    POST /api/v1/proposals                    -> 201 {proposal}
    GET  /api/v1/queue                        -> 200 {items, next}
    POST /api/v1/decisions                    -> 201 {decision}
    GET  /api/v1/lineage/{domain}/{subject}   -> 200 {nodes, edges}
    GET  /api/v1/releases                     -> 200 {items}
    POST /api/v1/releases                     -> 201 {release}
    POST /api/v1/transforms/preview           -> 200 {preview}
    GET  /api/v1/transforms                   -> 200 {items}

`curation_api` returns a Starlette app for `rx.App(api_transformer=...)`, so the Reflex
process serves these routes on its own port.

A token names an actor and its kind (`tokens.py`). Every write goes through a
`LedgerClaimStore` typed as that actor. The ledger refuses an agent decision without an
approved policy, returned as a 403 naming the policy. An agent may not cut a release. A
repeat that writes nothing answers 200.

A proposal's reasoning, alternatives, triage, ``reasons``, ``column`` and ``source_profile``
are stored as a ``proposal`` param on its run; the queue reads them back. A claim with no
such run is triaged from its confidence.

``POST /proposals`` accepts ``X-Curation-Directed-By: Bearer <token>`` naming the human who
directed an agent: 401 if unknown, 403 if not human, both before any write.

A mapping claim's ``transform`` is shown applied to its column's kept cells.
``POST /transforms/preview`` runs another reading and writes nothing. A proposal's triage is
raised to what its transform asks of a reviewer. ``derived_from`` names the claim a curator
edited a proposal from.

Writes are serialised by one lock: lamindb's ``ln.context.run`` is process-wide.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import ClaimDomain, DecisionKind, Outcome
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ..curate import session
from ..domain import transform
from ..domain.marker import MARKER_DOMAIN
from ..domain.reagent_mapping import REAGENT_MAPPING_DOMAIN, NeedsAHuman
from ..propose.columns import candidate_chains, raised
from ..propose.role import as_claim
from ..propose.schema import (
    _ATTENTION_RANK,
    Attention,
    Candidate,
    Proposal,
    ProposalRequest,
    Reason,
    Triage,
    assess,
    ranked,
    review_order,
)
from .tokens import Actor, Tokens

#: The domains served.
DOMAINS: dict[str, ClaimDomain] = {d.name: d for d in (MARKER_DOMAIN, REAGENT_MAPPING_DOMAIN)}

#: The ledger constraint an agent decision without an approved policy fails.
POLICY_RULE = "agent_decides_under_an_approved_policy"

#: The rule an agent release would break.
RELEASE_RULE = "a release is the curator role's (migration 0005 grants)"

#: The header naming the curator who directed an agent's proposal.
DIRECTED_BY_HEADER = "X-Curation-Directed-By"
DIRECTED_BY_RULE = "directed_by_is_a_human"

QUEUE_LIMIT = 50
QUEUE_LIMIT_MAX = 500


class Refused(Exception):
    """An answer other than success, with its status and its JSON body."""

    def __init__(self, status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status = status
        self.body = {"error": error, **extra}


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def curation_api(*, tokens: Tokens, branch: str,
                 domains: Mapping[str, ClaimDomain] = DOMAINS,
                 today: Callable[[], str] = _today) -> Starlette:
    """The v1 routes, for Reflex's ``api_transformer``.

    ``branch`` is where subjects and their review state live; no default. ``today`` dates
    decisions.
    """
    api = _Api(tokens=tokens, branch=branch, domains=dict(domains), today=today)
    return Starlette(routes=[Mount("/api/v1", routes=[
        Route("/proposals", api.endpoint(api.propose, body=True), methods=["POST"]),
        Route("/queue", api.endpoint(api.queue), methods=["GET"]),
        Route("/decisions", api.endpoint(api.decide, body=True), methods=["POST"]),
        Route("/lineage/{domain}/{subject}", api.endpoint(api.lineage), methods=["GET"]),
        Route("/releases", api.endpoint(api.releases), methods=["GET"]),
        Route("/releases", api.endpoint(api.release, body=True), methods=["POST"]),
        Route("/transforms/preview", api.endpoint(api.preview, body=True), methods=["POST"]),
        Route("/transforms", api.endpoint(api.transforms), methods=["GET"]),
    ])])


class _Api:
    def __init__(self, *, tokens: Tokens, branch: str, domains: dict[str, ClaimDomain],
                 today: Callable[[], str]) -> None:
        self.tokens, self.branch, self.domains, self.today = tokens, branch, domains, today
        self.writing = threading.Lock()

    def endpoint(self, handler, *, body: bool = False):
        """An async route over a blocking ``handler(actor, request, payload)``, run in a thread.

        lamindb is Django, which refuses a synchronous query from a thread running an event loop.
        """
        async def route(request: Request) -> JSONResponse:
            try:
                actor = self._actor(request)
                payload = await _json(request) if body else {}
                status, answer = await run_in_threadpool(handler, actor, request, payload)
            except Refused as refused:
                return JSONResponse(refused.body, status_code=refused.status)
            return JSONResponse(answer, status_code=status)
        return route

    def _actor(self, request: Request) -> Actor:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        actor = self.tokens.actor(token.strip()) if scheme.lower() == "bearer" else None
        if actor is None:
            raise Refused(401, "a bearer token naming a known actor is required; "
                               "an anonymous request is refused")
        return actor

    def _domain(self, name: str) -> ClaimDomain:
        if name not in self.domains:
            raise Refused(404, f"no domain {name!r}; served: {sorted(self.domains)}")
        return self.domains[name]

    def _store(self, actor: Actor, domain: ClaimDomain):
        from clinicalomx_ledger.grammar.store import LedgerClaimStore

        return LedgerClaimStore(self.branch, domain, **actor.session())

    # -- proposals -----------------------------------------------------------------------

    def propose(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter

        domain = self._domain(_field(body, "domain", str))
        subject = _field(body, "subject", str)
        evidence = _field(body, "evidence", dict)
        derived = self._derived_from(body)
        try:
            proposal = _proposal(body)
            claim = _claim(domain, subject, proposal, evidence)
            sheet = {**derived, **_sheet_of(body)}
            shown = _transform(proposal.value, sheet.get("column")) if (
                domain is REAGENT_MAPPING_DOMAIN) else None
        except (ValueError, TypeError, KeyError) as refused:
            raise Refused(422, f"not a proposal: {refused}") from refused
        if shown is not None:
            proposal = _raised_to(proposal, shown)
            sheet["transform"] = shown
        if derived:
            sheet["derived_from"] = body["derived_from"]
        working = {**_working(subject, proposal), **sheet}
        directed_by = self._directed_by(request)
        params = {"api": "v1", "proposal": json.dumps(working)}
        if directed_by is not None:
            params["directed_by"] = directed_by
        with self.writing:
            before = {c.uid for c in adapter.claim_log(domain.name, subject)}
            with session.opened(self.branch, params):
                try:
                    self._store(actor, domain).add(claim)
                except ValueError as refused:
                    raise Refused(422, str(refused)) from refused
            row = _row_of(adapter.claim_log(domain.name, subject, str(claim.predicate)), claim)
        if row.uid in before:
            stored = _workings([row]).get(row.uid) or _working(subject, _proposal_of(row, None))
            return 200, {"proposal": {**_proposal_json(row, stored),
                                      "directed_by": _run_param(row, "directed_by")}}
        return 201, {"proposal": {**_proposal_json(row, working), "directed_by": directed_by}}

    def _derived_from(self, body: dict) -> dict:
        """The sheet profile of the claim ``derived_from`` names; empty without one."""
        from clinicalomx_ledger import adapter

        uid = body.get("derived_from")
        if uid is None:
            return {}
        row = adapter.claim(uid) if isinstance(uid, str) else None
        if row is None:
            raise Refused(422, f"derived_from names no claim {uid!r}")
        working = _workings([row]).get(row.uid) or {}
        return {k: working[k] for k in ("column", "source_profile") if k in working}

    def _directed_by(self, request: Request) -> str | None:
        """The human named by ``X-Curation-Directed-By``, or None when the header is absent."""
        header = request.headers.get(DIRECTED_BY_HEADER)
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        actor = self.tokens.actor(token.strip()) if scheme.lower() == "bearer" else None
        if actor is None:
            raise Refused(401, f"{DIRECTED_BY_HEADER} names no known actor")
        if actor.kind != "human":
            raise Refused(403, f"{DIRECTED_BY_HEADER} names {actor.name}, an {actor.kind}; "
                               "only a human directs an agent", rule=DIRECTED_BY_RULE)
        return actor.name

    # -- queue ---------------------------------------------------------------------------

    def queue(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter

        wanted = request.query_params.get("domain")
        domains = [self._domain(wanted)] if wanted else list(self.domains.values())
        limit = _int(request.query_params.get("limit"), QUEUE_LIMIT, "limit")
        after = _cursor(request.query_params.get("next"))
        if not 1 <= limit <= QUEUE_LIMIT_MAX:
            raise Refused(422, f"limit must be between 1 and {QUEUE_LIMIT_MAX}")
        rows = sorted((row for d in domains
                       for row in adapter.queue(d.name, multi_valued=d.multi_valued_predicates)),
                      key=lambda row: row.uid)
        workings = _workings(rows)
        pairs, row_of = [], {}
        for row in rows:
            proposal = _proposal_of(row, workings.get(row.uid))
            row_of[id(proposal)] = row
            pairs.append((row.subject, proposal))
        keyed = [(_key(s, p, row_of[id(p)].uid), s, p) for s, p in review_order(pairs)]
        remaining = [k for k in keyed if after is None or k[0] > after]
        page = remaining[:limit]
        items = [_proposal_json(row_of[id(p)], workings.get(row_of[id(p)].uid)
                                or _working(s, p)) for _, s, p in page]
        more = len(remaining) > limit
        return 200, {"items": items, "next": json.dumps(page[-1][0]) if more else None}

    # -- decisions -----------------------------------------------------------------------

    def decide(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter
        from django.db import IntegrityError

        uid = _field(body, "claim_id", str)
        reason = body.get("reason") or ""
        if not isinstance(reason, str):
            raise Refused(422, "reason is a string")
        try:
            outcome = Outcome(_field(body, "outcome", str))
        except ValueError as refused:
            raise Refused(422, f"outcome is one of {[o.value for o in Outcome]}") from refused
        row = adapter.claim(uid)
        if row is None or row.domain not in self.domains:
            raise Refused(404, f"no claim {uid!r}")
        domain = self.domains[row.domain]
        store = self._store(actor, domain)
        claim = store.claim(uid)
        kind = DecisionKind.ONE_OFF if outcome == Outcome.REJECTED else ""
        judgement = Judgement(by=actor.name, at=self.today(), kind=kind, reason=reason)
        with self.writing:
            with session.opened(self.branch, {"api": "v1", "decision": uid}):
                try:
                    made = store.decide(claim, outcome=outcome, judgement=judgement)
                except IntegrityError as refused:
                    raise _policy_refusal(actor, refused) from refused
                except NeedsAHuman as refused:
                    raise Refused(403, str(refused), rule=NeedsAHuman.rule,
                                  policy=actor.policy_version or None) from refused
                except ValueError as refused:
                    raise Refused(422, str(refused)) from refused
            log = adapter.decision_log(domain.name, row.subject, row.predicate)
        same = _same_claim(row, domain)
        decision = next(d for d in reversed(log) if d.claim_uid in same)
        return (201 if made else 200), {"decision": _decision_json(decision)}

    # -- lineage -------------------------------------------------------------------------

    def lineage(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter

        domain = self._domain(request.path_params["domain"])
        subject = request.path_params["subject"]
        claims = adapter.claim_log(domain.name, subject)
        decisions = adapter.decision_log(domain.name, subject)
        in_effect = adapter.answers(domain.name, subject)
        answering = {d.uid for d in in_effect}
        workings = _workings(claims)
        outcome = {d.claim_uid: d.outcome for d in decisions}
        usage = self._usage() if domain is REAGENT_MAPPING_DOMAIN else []
        entity = f"entity:{domain.name}:{subject}"
        nodes: dict[str, dict] = {entity: {"id": entity, "kind": "entity", "label": subject,
                                           "data": _slots(domain, claims, in_effect)}}
        edges: list[dict] = []
        sheets: dict[str, tuple[str, dict]] = {}
        for c in claims:
            source = f"source:{c.source}:{c.source_id}:{c.source_version}"
            working = workings.get(c.uid) or {}
            nodes.setdefault(source, {
                "id": source, "kind": "source",
                "label": " ".join(filter(None, (c.source, c.source_id, c.source_version)))})
            if working.get("column"):
                nodes[source]["data"] = {**working["column"], "level": "column",
                                         "claimed": True}
            if working.get("source_profile"):
                sheets[c.source_version] = (c.source, working["source_profile"])
            # A claim node's id is the claim id.
            nodes[c.uid] = {
                "id": c.uid, "kind": "claim", "label": f"{c.predicate} = {c.value}",
                "claim_id": c.uid, "confidence": c.confidence, "proposed_by": c.proposed_by,
                "actor_kind": c.actor_kind, "run": c.run_uid,
                "data": {"reasons": working.get("reasons", []), "proposed_by": c.proposed_by,
                         "actor_kind": c.actor_kind, "agent_model": c.agent_model,
                         "confidence": c.confidence, "rivals": working.get("alternatives", []),
                         "triage": working.get("triage"), "outcome": outcome.get(c.uid),
                         **_shown(working, usage)}}
            edges.append({"from": source, "to": c.uid})
        for version, (source, sheet) in sheets.items():
            _add_sheet(nodes, edges, source, version, sheet)
        for d in decisions:
            node = f"decision:{d.uid}"
            # The decision's own `kind` is `decision_kind` here; `kind` is the node's.
            nodes[node] = {**_decision_json(d), "decision_kind": d.kind, "id": node,
                           "kind": "decision", "label": f"{d.outcome} by {d.by}",
                           "in_effect": d.uid in answering}
            edges.append({"from": d.claim_uid, "to": node})
            if d.uid in answering:
                edges.append({"from": node, "to": entity})
        return 200, {"nodes": list(nodes.values()), "edges": edges}

    # -- transforms ----------------------------------------------------------------------

    def preview(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        """A reading run over the column a claim was read from. Writes nothing."""
        from clinicalomx_ledger import adapter

        uid = _field(body, "claim_id", str)
        value = _field(body, "value", str)
        row = adapter.claim(uid)
        if row is None or row.domain not in self.domains:
            raise Refused(404, f"no claim {uid!r}")
        column = (_workings([row]).get(row.uid) or {}).get("column")
        try:
            shown = _transform(value, column)
        except ValueError as refused:
            raise Refused(422, f"not a transform: {refused}") from refused
        if shown is None or "produced" not in shown:
            raise Refused(422, f"claim {uid} kept no cells of column {row.source_id!r}, or "
                               f"{value!r} reads another column, so there is nothing to run")
        return 200, {"preview": {**shown, **_usage_of(self._usage(), shown["chain"])}}

    def transforms(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        """Every accepted chain, how often and on which sheets, most accepted first."""
        return 200, {"items": [_usage_json(u) for u in self._usage()]}

    def _usage(self) -> list[transform.Usage]:
        from clinicalomx_ledger import adapter

        # One claim read per accepted mapping decision.
        rows = [adapter.claim(d.claim_uid)
                for d in adapter.answers(REAGENT_MAPPING_DOMAIN.name)]
        return transform.usage((r.subject, r.value) for r in rows if r is not None)

    # -- releases ------------------------------------------------------------------------

    def releases(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter

        wanted = request.query_params.get("domain")
        if wanted:
            self._domain(wanted)
        rows = [r for r in adapter.releases(wanted or None) if r.domain in self.domains]
        return 200, {"items": [_release_json(r) for r in rows]}

    def release(self, actor: Actor, request: Request, body: dict) -> tuple[int, dict]:
        from clinicalomx_ledger import adapter

        domain = self._domain(_field(body, "domain", str))
        if actor.kind == "agent":
            raise Refused(403, f"{actor.name} is an agent, and the agent role proposes and "
                               "nothing else: releases belong to the curator role",
                          rule=RELEASE_RULE)
        with self.writing:
            with session.opened(self.branch, {"api": "v1", "release": domain.name}) as run:
                cut = adapter.cut_release(
                    domain=domain.name, by=actor.name, actor_kind=actor.kind,
                    agent_model=actor.agent_model,
                    agent_model_version=actor.agent_model_version,
                    policy_version=actor.policy_version,
                    run_uid=None if run is None else str(run.uid))
        return 201, {"release": _release_json(cut)}


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as refused:
        raise Refused(400, "the body is not JSON") from refused
    if not isinstance(body, dict):
        raise Refused(422, "the body is a JSON object")
    return body


def _field(body: dict, name: str, kind: type):
    value = body.get(name)
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise Refused(422, f"{name} is required, as a {'string' if kind is str else 'object'}")
    return value


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as refused:
        raise Refused(422, f"{name} is an integer") from refused


def _key(subject: str, proposal: Proposal, uid: str) -> list:
    """A queued claim's place in `review_order`, with its uid to break a tie."""
    return [_ATTENTION_RANK[proposal.triage.label], proposal.confidence, proposal.margin,
            subject, uid]


def _cursor(raw: str | None) -> list | None:
    """The key of the last item a page served: the next page starts after it."""
    if raw is None:
        return None
    try:
        key = json.loads(raw)
    except ValueError as refused:
        raise Refused(422, "next is a cursor a queue page returned") from refused
    if not (isinstance(key, list) and len(key) == 5 and isinstance(key[0], int)
            and all(isinstance(k, (int, float)) for k in key[1:3])
            and all(isinstance(k, str) for k in key[3:])):
        raise Refused(422, "next is a cursor a queue page returned")
    return key


def _add_sheet(nodes: dict[str, dict], edges: list[dict], source: str, version: str,
               sheet: dict) -> None:
    """The sheet's node, an edge to each column, and a node with its reason for every column it
    left unclaimed."""
    prefix = f"source:{source}:"
    suffix = f":{version}"
    node = f"sheet:{source}:{version}"
    nodes[node] = {"id": node, "kind": "source",
                   "label": " ".join(filter(None, (sheet["file"], sheet["sheet"]))),
                   "data": {**{k: v for k, v in sheet.items() if k != "unclaimed"},
                            "level": "file"}}
    for column in sheet["unclaimed"]:
        nodes.setdefault(f"{prefix}{column['address']}{suffix}", {
            "id": f"{prefix}{column['address']}{suffix}", "kind": "source",
            "label": f"{column['address']} no claim",
            "data": {**column, "level": "column", "claimed": False}})
    for column in [n for n in nodes if n.startswith(prefix) and n.endswith(suffix)]:
        edges.append({"from": node, "to": column})


def _slots(domain: ClaimDomain, claims, in_effect) -> dict:
    """Every reviewed predicate of the domain as a slot, and the values accepted into each."""
    value = {c.uid: (c.predicate, c.value) for c in claims}
    filled: dict[str, list[str]] = {}
    for d in in_effect:
        if d.claim_uid in value:
            predicate, v = value[d.claim_uid]
            filled.setdefault(predicate, []).append(v)
    slots = [{"slot": str(p), "filled": str(p) in filled, "values": filled.get(str(p), [])}
             for p in domain.reviewed_predicates]
    return {"slots": slots, "filled": sum(s["filled"] for s in slots), "of": len(slots)}


_COLUMN_FIELDS = {"header": str, "address": str, "sheet": str, "column": str, "type": str,
                  "rows": int, "blank": int, "unique": int, "top": list}
_SHEET_FIELDS = {"file": str, "sheet": str, "rows": int, "columns": int, "content_hash": str,
                 "unclaimed": list}


def _sheet_of(body: dict) -> dict:
    """The optional ``column`` and ``source_profile`` of a sheet proposal, shape-checked."""
    found = {}
    if body.get("column") is not None:
        found["column"] = _checked_column(body["column"], "column")
    if body.get("source_profile") is not None:
        sheet = _checked(body["source_profile"], _SHEET_FIELDS, "source_profile")
        for i, u in enumerate(sheet["unclaimed"]):
            _checked_column(u, f"source_profile.unclaimed[{i}]")
            if not isinstance(u.get("reason"), str) or not u["reason"].strip():
                raise ValueError(f"source_profile.unclaimed[{i}] carries no reason")
        found["source_profile"] = sheet
    return found


def _checked_column(raw, what: str) -> dict:
    column = _checked(raw, _COLUMN_FIELDS, what)
    for i, v in enumerate(column["top"]):
        if not (isinstance(v, dict) and isinstance(v.get("value"), str)
                and isinstance(v.get("count"), int) and isinstance(v.get("note", ""), str)):
            raise ValueError(f"{what}.top[{i}] is {{value, count, note?}}")
    for i, v in enumerate(column.get("cells") or []):
        if not (isinstance(v, dict) and isinstance(v.get("value"), str)
                and type(v.get("count")) is int and v["count"] > 0):
            raise ValueError(f"{what}.cells[{i}] is {{value, count}}")
    return column


def _checked(raw, fields: dict[str, type], what: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} is an object")
    for name, kind in fields.items():
        if not isinstance(raw.get(name), kind) or isinstance(raw.get(name), bool):
            raise ValueError(f"{what}.{name} is required, as {kind.__name__}")
    return raw


def _transform(value: str, column: dict | None) -> dict | None:
    """``value`` as the transform a reviewer is shown, run over ``column``'s kept cells when it
    reads that column alone. None when ``value`` names no transform. Raises when a ``|`` value
    does not parse."""
    try:
        reading = transform.parse(value)
    except ValueError:
        if "|" in value:
            raise
        return None
    label, why = reading.attention()
    shown = {"reading": reading.text(), "chain": reading.chain, "attention": label.value,
             "why": why, "bespoke": reading.bespoke}
    cells = (column or {}).get("cells")
    if not isinstance(cells, list) or reading.columns != ((column or {}).get("address"),):
        return shown
    counted = transform.preview_counted(
        reading, (((str(c["value"]),), int(c["count"])) for c in cells))
    chains = [*candidate_chains(tuple(str(c["value"]) for c in cells)),
              transform.Step.of("trim").text(), transform.Step.of("casefold").text(),
              transform.IDENTITY]
    return {**shown, "produced": counted.produced, "rows": counted.rows,
            "complete": bool(column.get("cells_complete", True)),
            "examples": [{"input": i, "output": o} for i, o in counted.examples],
            "choices": [{"chain": c, "reading": transform.reading(reading.columns, c).text()}
                        for c in chains]}


def _raised_to(proposal: Proposal, shown: dict) -> Proposal:
    """``proposal`` with its triage raised to what its transform asks of a reviewer."""
    return replace(proposal, triage=raised(proposal.triage, shown["chain"]))


def _usage_of(usage: list[transform.Usage], chain: str) -> dict:
    found = next((u for u in usage if u.chain == chain), None)
    return {"accepted": found.accepted if found else 0,
            "accepted_on": list(found.sources) if found else []}


def _usage_json(u: transform.Usage) -> dict:
    return {"chain": u.chain, "accepted": u.accepted, "sources": list(u.sources),
            "bespoke": u.bespoke, "promotion_candidate": u.promotion_candidate}


def _shown(working: dict, usage: list[transform.Usage]) -> dict:
    """A claim node's ``transform``, or nothing."""
    shown = working.get("transform")
    return {"transform": {**shown, **_usage_of(usage, shown["chain"])}} if shown else {}


def _proposal(body: dict) -> Proposal:
    """The request body as a validated `Proposal`."""
    alternatives = ranked(Candidate(value=a["value"], confidence=a["confidence"],
                                    reasoning=a.get("reasoning", ""))
                          for a in body.get("alternatives") or ())
    confidence = body.get("confidence")
    triage = body.get("triage")
    return Proposal(
        predicate=_field(body, "predicate", str), value=_field(body, "value", str),
        confidence=confidence,
        triage=(Triage(Attention(triage["label"]), triage["reason"]) if triage
                else assess(confidence, alternatives)),
        reasoning=body.get("reasoning") or "", alternatives=alternatives,
        reasons=tuple(Reason.from_json(r) for r in body.get("reasons") or ()))


def _claim(domain: ClaimDomain, subject: str, proposal: Proposal, evidence: dict):
    """The claim a proposal asserts, built the way this domain's CLI path builds it."""
    source = {k: evidence.get(k) or "" for k in ("source", "source_id", "source_version")}
    text = evidence.get("text") or ""
    if domain is MARKER_DOMAIN:
        return as_claim(proposal, ProposalRequest(marker=subject, evidence=text),
                        evidence=text, **source)
    return domain.claim(subject=subject, predicate=proposal.predicate, value=proposal.value,
                        evidence=text, confidence=proposal.confidence, **source)


def _working(subject: str, proposal: Proposal) -> dict:
    """What a proposal says beyond its claim, as stored on its run."""
    return {"subject": subject, "predicate": str(proposal.predicate), "value": proposal.value,
            "reasoning": proposal.reasoning,
            "reasons": [r.to_json() for r in proposal.reasons],
            "triage": {"label": proposal.triage.label.value, "reason": proposal.triage.reason},
            "alternatives": [{"value": a.value, "confidence": a.confidence,
                              "reasoning": a.reasoning} for a in proposal.alternatives]}


def _run_param(row, name: str) -> Any:
    """One param of the run ``row`` cites, or None."""
    if not row.run_uid:
        return None
    import lamindb as ln

    run = ln.Run.filter(uid=row.run_uid).one_or_none()
    return None if run is None else (run.params or {}).get(name)


def _workings(rows) -> dict[str, dict]:
    """Claim uid -> the working its run recorded, for the rows whose run recorded one."""
    runs = {r.run_uid for r in rows if r.run_uid}
    if not runs:
        return {}
    import lamindb as ln

    params = {run.uid: (run.params or {}).get("proposal")
              for run in ln.Run.filter(uid__in=runs)}
    found = {}
    for row in rows:
        raw = params.get(row.run_uid)
        working = json.loads(raw) if isinstance(raw, str) else None
        if working and (working["subject"], working["predicate"], working["value"]) == (
                row.subject, row.predicate, row.value):
            found[row.uid] = working
    return found


def _proposal_of(row, working: dict | None) -> Proposal:
    """A queued claim as the `Proposal` `review_order` sorts, rebuilt from what was stored."""
    confidence = 0.0 if row.confidence is None else row.confidence
    if working is not None:
        alternatives = tuple(Candidate(**a) for a in working["alternatives"])
        triage = Triage(Attention(working["triage"]["label"]), working["triage"]["reason"])
        reasoning = working["reasoning"]
        reasons = tuple(Reason.from_json(r) for r in working.get("reasons", ()))
    else:
        alternatives, reasoning, reasons = (), "", ()
        triage = (assess(confidence) if row.confidence is not None
                  else Triage(Attention.SCRUTINISE, "no confidence was stated"))
    return Proposal(predicate=row.predicate, value=row.value, confidence=confidence,
                    triage=triage, reasoning=reasoning, alternatives=alternatives,
                    reasons=reasons)


def _row_of(log, claim):
    """The newest ledger row asserting ``claim``."""
    return next(r for r in reversed(log)
                if (r.source, r.source_id, r.value, r.source_version)
                == (str(claim.source), claim.source_id, claim.value, claim.source_version))


def _same_claim(row, domain: ClaimDomain) -> set[str]:
    """Every row uid that is the same claim as ``row``."""
    from clinicalomx_ledger import adapter

    key = (row.source, row.source_id, row.value, row.source_version)
    return {r.uid for r in adapter.claim_log(domain.name, row.subject, row.predicate)
            if (r.source, r.source_id, r.value, r.source_version) == key}


def _policy_refusal(actor: Actor, refused: Exception) -> Refused:
    """The ledger's refusal of an agent decision, as a 403 naming the policy."""
    text = str(refused)
    if POLICY_RULE in text:
        why = f"{actor.name} cites no policy, and an agent decides only under an approved one"
    elif "policy" in text:
        why = (f"{actor.name} cites policy {actor.policy_version!r}, "
               "which no human has approved")
    else:
        raise refused
    return Refused(403, f"the ledger refused this decision: {why}",
                   policy=actor.policy_version or None, rule=POLICY_RULE)


def _at(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _proposal_json(row, working: dict) -> dict:
    return {
        "claim_id": row.uid, "domain": row.domain, "subject": row.subject,
        "predicate": row.predicate, "value": row.value,
        "evidence": {"source": row.source, "source_id": row.source_id,
                     "source_version": row.source_version, "text": row.evidence},
        "confidence": row.confidence, "alternatives": working["alternatives"],
        "triage": working["triage"], "reasoning": working["reasoning"],
        "reasons": working.get("reasons", []),
        **({"transform": working["transform"]} if working.get("transform") else {}),
        "proposed_by": row.proposed_by, "actor_kind": row.actor_kind,
        "agent_model": row.agent_model, "agent_model_version": row.agent_model_version,
        "policy_version": row.policy_version, "run": row.run_uid, "created_at": _at(row.created_at),
    }


def _decision_json(d) -> dict:
    return {
        "decision_id": d.uid, "claim_id": d.claim_uid, "outcome": d.outcome, "reason": d.reason,
        "kind": d.kind, "by": d.by, "actor_kind": d.actor_kind, "agent_model": d.agent_model,
        "agent_model_version": d.agent_model_version, "policy": d.policy_version,
        "decided_at": _at(d.decided_at), "run": d.run_uid, "created_at": _at(d.created_at),
    }


def _release_json(r) -> dict:
    return {
        "release_id": r.uid, "domain": r.domain, "manifest_hash": r.manifest_hash,
        "decisions": r.entries, "by": r.by, "actor_kind": r.actor_kind,
        "agent_model": r.agent_model, "agent_model_version": r.agent_model_version,
        "policy_version": r.policy_version, "run": r.run_uid, "created_at": _at(r.created_at),
    }
