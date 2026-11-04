"""The curation API client this UI reads through. It never imports the ledger or markers.

A queue item carries ``claim_id``; ``triage`` is ``{label, reason}``; an alternative is
``{value, confidence, reasoning}``; a 403 names its policy under ``policy``, else ``detail``. A
payload missing a field the page draws raises `ContractError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

PREFIX = "/api/v1"
#: The lineage graph's node kinds, left to right.
KINDS = ("source", "claim", "decision", "entity")
OUTCOMES = ("accepted", "rejected")


class ContractError(ValueError):
    """The API answered with a shape the v1 contract does not describe."""


class NoActor(Exception):
    """A decision came with no curator token, so no actor is identified. Nothing was sent."""

    def __str__(self) -> str:
        return "no actor is identified: enter your API token before deciding"


class UnknownActor(NoActor):
    """A token was given and the API named no actor for it (401)."""

    def __str__(self) -> str:
        return "your API token identifies no known actor: check it and enter it again"


@dataclass
class Refused(Exception):
    """The API refused a decision. ``rule`` is the refusing rule and ``policy`` the actor's policy,
    each as the API named it, or empty."""

    status: int
    policy: str
    detail: str
    rule: str = ""

    def __str__(self) -> str:
        # The rule leads.
        if self.rule:
            said = f"refused by rule {self.rule!r}" + (
                f" under policy {self.policy!r}" if self.policy else ""
            )
        elif self.policy:
            said = f"refused by policy {self.policy!r}"
        else:
            return f"refused ({self.status})" + (
                f": {self.detail}" if self.detail else ": no reason given"
            )
        return said + (f": {self.detail}" if self.detail else "")


def _need(obj: dict, key: str, what: str):
    if not isinstance(obj, dict) or key not in obj:
        raise ContractError(f"{what} carries no {key!r}")
    return obj[key]


@dataclass
class Alternative:
    value: str
    confidence: str
    reasoning: str = ""


@dataclass
class ReasonRow:
    """One typed reason, with its weight as the page prints it."""

    kind: str
    #: ``0.88``; ``0.00`` for a check that ran and failed; ``-0.09`` held back; ``n/a`` unchecked.
    weight: str
    #: ``passed``, ``failed``, ``held back`` or ``not applicable``, as the API derived it.
    outcome: str
    evidence: str
    #: The check a not-applicable reason could not run; blank otherwise.
    check: str = ""


@dataclass
class ValueRow:
    value: str
    count: int
    note: str = ""


@dataclass
class ColumnView:
    """A sheet column as its lineage node describes it. Every number is counted from the file."""

    header: str
    address: str
    type: str
    rows: int
    blank: int
    unique: int
    top: list[ValueRow] = field(default_factory=list)


@dataclass
class SlotRow:
    slot: str
    filled: bool
    values: str = ""


@dataclass
class EntityView:
    label: str
    filled: int
    of: int
    slots: list[SlotRow] = field(default_factory=list)


@dataclass
class ExampleRow:
    input: str
    output: str


@dataclass
class ChoiceRow:
    """One library chain worth trying on a column, and the reading it makes of the claim's."""

    chain: str
    reading: str


@dataclass
class TransformView:
    """A mapping claim's transform applied to the column's rows. ``produced`` and ``rows`` are -1
    where the API kept no cells."""

    reading: str
    chain: str
    attention: str
    why: str
    bespoke: bool = False
    produced: int = -1
    rows: int = -1
    #: False where the column had more distinct cells than the API keeps.
    complete: bool = True
    examples: list[ExampleRow] = field(default_factory=list)
    choices: list[ChoiceRow] = field(default_factory=list)
    accepted: int = 0
    accepted_on: str = ""


def transform_view(raw: dict, what: str) -> TransformView:
    counted = "produced" in raw
    return TransformView(
        reading=str(_need(raw, "reading", what)),
        chain=str(_need(raw, "chain", what)),
        attention=str(_need(raw, "attention", what)),
        why=str(_need(raw, "why", what)),
        bespoke=bool(raw.get("bespoke")),
        produced=int(raw["produced"]) if counted else -1,
        rows=int(_need(raw, "rows", what)) if counted else -1,
        complete=bool(raw.get("complete", True)),
        examples=[
            ExampleRow(input=str(_need(e, "input", what)), output=str(_need(e, "output", what)))
            for e in raw.get("examples") or []
        ],
        choices=[
            ChoiceRow(chain=str(_need(c, "chain", what)), reading=str(_need(c, "reading", what)))
            for c in raw.get("choices") or []
        ],
        accepted=int(raw.get("accepted") or 0),
        accepted_on=", ".join(str(v) for v in raw.get("accepted_on") or []),
    )


@dataclass
class QueueRow:
    """One undecided claim as the page draws it."""

    claim_id: str
    domain: str
    subject: str
    predicate: str
    value: str
    #: What the claim cites: ``source``, ``source_id``, ``source_version``, then the quoted text.
    source: str
    evidence: str
    confidence: str
    triage: str
    triage_reason: str
    #: The proposer's justification. Display only; the ledger stores none.
    reasoning: str = ""
    alternatives: list[Alternative] = field(default_factory=list)
    #: Typed reasons. Empty where the proposer stated none.
    reasons: list[ReasonRow] = field(default_factory=list)
    proposed_by: str = ""
    actor_kind: str = ""
    #: The three parts of ``source``.
    source_name: str = ""
    source_id: str = ""
    source_version: str = ""


def _citation(evidence: dict, what: str) -> str:
    parts = [
        str(_need(evidence, k, f"{what}'s evidence"))
        for k in ("source", "source_id", "source_version")
    ]
    return " / ".join(parts)


def queue_rows(payload: dict) -> list[QueueRow]:
    """The queue's items in the order the API returned them."""
    rows = []
    for i, item in enumerate(_need(payload, "items", "the queue")):
        what = f"queue item {i}"
        triage = _need(item, "triage", what)
        rows.append(
            QueueRow(
                claim_id=str(_need(item, "claim_id", what)),
                domain=str(_need(item, "domain", what)),
                subject=str(_need(item, "subject", what)),
                predicate=str(_need(item, "predicate", what)),
                value=str(_need(item, "value", what)),
                source=_citation(_need(item, "evidence", what), what),
                evidence=str(_need(item["evidence"], "text", f"{what}'s evidence")),
                confidence=f"{float(_need(item, 'confidence', what)):.2f}",
                triage=str(_need(triage, "label", f"{what}'s triage")),
                triage_reason=str(_need(triage, "reason", f"{what}'s triage")),
                reasoning=str(item.get("reasoning") or ""),
                alternatives=[
                    Alternative(
                        value=str(_need(a, "value", f"{what}'s alternative")),
                        confidence=f"{float(_need(a, 'confidence', f'{what} alternative')):.2f}",
                        reasoning=str(a.get("reasoning") or ""),
                    )
                    for a in _need(item, "alternatives", what)
                ],
                reasons=reason_rows(item.get("reasons") or [], what),
                proposed_by=str(item.get("proposed_by") or ""),
                actor_kind=str(item.get("actor_kind") or ""),
                source_name=str(item["evidence"].get("source") or ""),
                source_id=str(item["evidence"].get("source_id") or ""),
                source_version=str(item["evidence"].get("source_version") or ""),
            )
        )
    return rows


def reason_rows(reasons: list, what: str) -> list[ReasonRow]:
    rows = []
    for r in reasons:
        weight = _need(r, "weight", f"{what}'s reason")
        rows.append(
            ReasonRow(
                kind=str(_need(r, "kind", f"{what}'s reason")),
                weight="n/a" if weight is None else f"{float(weight):.2f}",
                outcome=str(_need(r, "outcome", f"{what}'s reason")),
                evidence=str(_need(r, "evidence", f"{what}'s reason")),
                check=str(r.get("check") or ""),
            )
        )
    return rows


def _data(node: dict) -> dict:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def column_of(payload: dict, claim_id: str) -> ColumnView | None:
    """The sheet column ``claim_id`` was read from, or None where its source is not a column."""
    by_id = {n.get("id"): n for n in payload.get("nodes", []) if isinstance(n, dict)}
    for e in payload.get("edges", []):
        node = by_id.get(e.get("from"), {})
        data = _data(node)
        if (
            e.get("to") == claim_id
            and node.get("kind") == "source"
            and data.get("level") == ("column")
        ):
            what = f"column node {node.get('id')!r}"
            return ColumnView(
                header=str(_need(data, "header", what)),
                address=str(_need(data, "address", what)),
                type=str(_need(data, "type", what)),
                rows=int(_need(data, "rows", what)),
                blank=int(_need(data, "blank", what)),
                unique=int(_need(data, "unique", what)),
                top=[
                    ValueRow(
                        value=str(_need(v, "value", what)),
                        count=int(_need(v, "count", what)),
                        note=str(v.get("note") or ""),
                    )
                    for v in _need(data, "top", what)
                ],
            )
    return None


def transform_of(payload: dict, claim_id: str) -> TransformView | None:
    """The transform ``claim_id``'s node carries, or None where it reads no transform."""
    for n in payload.get("nodes", []):
        if isinstance(n, dict) and n.get("id") == claim_id and n.get("kind") == "claim":
            raw = _data(n).get("transform")
            return transform_view(raw, f"claim node {claim_id!r}'s transform") if raw else None
    return None


def entity_of(payload: dict) -> EntityView | None:
    """The entity node's slots and how many are filled, or None where it carries no slots."""
    for n in payload.get("nodes", []):
        data = _data(n) if isinstance(n, dict) else {}
        if n.get("kind") == "entity" and "slots" in data:
            what = f"entity node {n.get('id')!r}"
            return EntityView(
                label=str(n.get("label") or ""),
                filled=int(_need(data, "filled", what)),
                of=int(_need(data, "of", what)),
                slots=[
                    SlotRow(
                        slot=str(_need(s, "slot", what)),
                        filled=bool(_need(s, "filled", what)),
                        values=", ".join(str(v) for v in s.get("values") or []),
                    )
                    for s in _need(data, "slots", what)
                ],
            )
    return None


def refusal_of(response: httpx.Response) -> Refused:
    """A refused decision's rule and policy names, verbatim."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    detail = body.get("detail") or body.get("error")
    return Refused(
        status=response.status_code,
        policy=str(body.get("policy") or ""),
        detail="" if detail is None else str(detail),
        rule=str(body.get("rule") or ""),
    )


class Client:
    """One actor's view of the API. The token is the actor."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("CURATION_API_URL") or "http://localhost:8000"
        ).rstrip("/")
        self.token = token or ""
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.AsyncClient(
            base_url=self.base_url, headers=headers, transport=self._transport, timeout=10.0
        )

    def as_actor(self, token: str) -> Client:
        """The same API, reached the same way, under ``token``."""
        return Client(base_url=self.base_url, token=token, transport=self._transport)

    async def verify(self) -> None:
        """Check this token names a known actor with one cheap authenticated read.

        No token raises `NoActor` without a request; a 401 raises `UnknownActor`.
        """
        if not self.token:
            raise NoActor()
        async with self._http() as http:
            r = await http.get(f"{PREFIX}/queue", params={"limit": 1})
        if r.status_code == 401:
            raise UnknownActor()
        r.raise_for_status()

    async def queue(self) -> list[QueueRow]:
        """Every undecided claim, following ``next`` until the API stops giving one."""
        rows: list[QueueRow] = []
        url: str | None = f"{PREFIX}/queue"
        async with self._http() as http:
            while url:
                r = await http.get(url)
                r.raise_for_status()
                payload = r.json()
                rows.extend(queue_rows(payload))
                nxt = payload.get("next")
                # `next` is followed as an opaque link.
                url = str(nxt) if nxt else None
        return rows

    async def lineage(self, domain: str, subject: str) -> dict:
        async with self._http() as http:
            r = await http.get(
                f"{PREFIX}/lineage/{quote(domain, safe='')}/{quote(subject, safe='')}"
            )
            r.raise_for_status()
            return r.json()

    async def propose(self, body: dict, directed_by: str = "") -> dict:
        """POST one proposal. Returns it as the API echoes it, ``claim_id`` included.

        ``directed_by`` is the directing curator's token, sent as ``X-Curation-Directed-By``. A
        repeat returns 200 with the standing claim. A 403 raises `Refused`.
        """
        headers = {"X-Curation-Directed-By": f"Bearer {directed_by}"} if directed_by else {}
        async with self._http() as http:
            r = await http.post(f"{PREFIX}/proposals", json=body, headers=headers)
        if r.status_code == 403:
            raise refusal_of(r)
        r.raise_for_status()
        return _need(r.json(), "proposal", "a created proposal")

    async def preview(self, claim_id: str, value: str) -> TransformView:
        """``value`` run over the column ``claim_id`` was read from. Writes nothing."""
        async with self._http() as http:
            r = await http.post(
                f"{PREFIX}/transforms/preview", json={"claim_id": claim_id, "value": value}
            )
        if r.status_code == 422:
            raise ValueError(str(r.json().get("error") or r.text))
        r.raise_for_status()
        return transform_view(_need(r.json(), "preview", "a preview"), "the preview")

    async def decide(self, claim_id: str, outcome: str, reason: str) -> dict:
        """Record ``outcome`` on ``claim_id``. A 403 raises `Refused` carrying the policy name."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome is one of {OUTCOMES}, got {outcome!r}")
        if not self.token:
            raise NoActor()
        async with self._http() as http:
            r = await http.post(
                f"{PREFIX}/decisions",
                json={"claim_id": claim_id, "outcome": outcome, "reason": reason},
            )
        if r.status_code == 403:
            raise refusal_of(r)
        r.raise_for_status()
        return r.json()
