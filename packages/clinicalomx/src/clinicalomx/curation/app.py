"""The curation review page: the queue in API order, and each item's lineage as a graph.

Everything comes through `client.Client`. Mount it with `add_pages`, or build a whole app with
`make_app(api=)`. A decision is sent only under the token the curator typed. The queue is read on
load and after each decision; the page does not poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import reflex as rx

from clinicalomx.curation import client
from clinicalomx.curation.client import ColumnView, EntityView, QueueRow, TransformView
from clinicalomx.curation.graph import flow_graph
from clinicalomx.extract.app.web import tokens
from clinicalomx.extract.app.web.design import tokens as design

ROUTE = "/curation"
#: The panel's tabs, in review order.
TABS = ("why", "transform", "column", "rivals")
#: The Fills choice that places the column nowhere.
UNPLACED = ""


@dataclass
class FillOption:
    """One destination the Fills control offers, labelled with who put it there."""

    slot: str
    label: str


def fill_options(row: QueueRow | None, entity: EntityView | None) -> list[FillOption]:
    """What the column could fill: the proposed slot, each rival, the entity's other slots, then
    unplaced."""
    if row is None:
        return []
    shown = [FillOption(row.predicate, f"{row.predicate} - proposed, {row.confidence}")]
    shown += [FillOption(a.value, f"{a.value} - rival, {a.confidence}")
              for a in row.alternatives if a.value != row.predicate]
    named = {o.slot for o in shown}
    shown += [FillOption(s.slot, s.slot) for s in (entity.slots if entity else [])
              if s.slot not in named]
    return [*shown, FillOption(UNPLACED, "- nothing, leave unplaced -")]


class _Flow(rx.NoSSRComponent):
    library = "@xyflow/react@12.8.6"

    def add_imports(self) -> dict:
        return {"": "@xyflow/react/dist/style.css"}


class ReactFlow(_Flow):
    tag = "ReactFlow"
    nodes: rx.Var[list[dict[str, Any]]]
    edges: rx.Var[list[dict[str, Any]]]
    fit_view: rx.Var[bool]
    nodes_draggable: rx.Var[bool]
    nodes_connectable: rx.Var[bool]


class Background(_Flow):
    tag = "Background"


class Controls(_Flow):
    tag = "Controls"
    show_interactive: rx.Var[bool]


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TransportError):
        return f"cannot reach the curation API at {client.Client().base_url}: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the API answered {exc.response.status_code}: {exc.response.text[:300]}"
    return f"{type(exc).__name__}: {exc}"


class CurationState(rx.State):
    items: list[QueueRow] = []
    queue_error: str = ""
    selected: str = ""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    lineage_error: str = ""
    reason: str = ""
    #: The last refusal or failure for the selected claim, its rule and policy names verbatim.
    refusal: str = ""
    notice: str = ""
    token: str = ""
    tab: str = "why"
    #: The sheet column the selected claim was read from, from its lineage node's ``data``.
    column: ColumnView | None = None
    #: The entity the claims fill, with how many of its slots are filled.
    entity: EntityView | None = None
    #: The selected claim's transform as the API ran it, or None where it reads none.
    transform: TransformView | None = None
    #: The destination and reading the curator is editing; the claim's own until changed.
    edit_slot: str = ""
    edit_value: str = ""
    #: The edited reading run over the same rows, once the curator asks for it.
    edited: TransformView | None = None
    #: The reading ``edited`` was run on, as it was sent.
    previewed: str = ""
    edit_error: str = ""

    def _client(self) -> client.Client:
        return client.Client(token=self.token.strip())

    @rx.var
    def current(self) -> QueueRow | None:
        return next((r for r in self.items if r.claim_id == self.selected), None)

    @rx.var
    def fills(self) -> list[FillOption]:
        row = next((r for r in self.items if r.claim_id == self.selected), None)
        return fill_options(row, self.entity)

    async def _refresh(self) -> None:
        try:
            self.items = await self._client().queue()
            self.queue_error = ""
        except (httpx.HTTPError, client.ContractError, ValueError) as exc:
            self.queue_error = _describe(exc)

    async def _load(self) -> None:
        """Check the token names a known actor, then read the queue under it."""
        try:
            await self._client().verify()
        except client.NoActor as exc:
            self.items, self.queue_error = [], str(exc)
            await self._select("")
            return
        except (httpx.HTTPError, ValueError) as exc:
            self.queue_error = _describe(exc)
            return
        await self._refresh()
        if self.items and not self.current:
            await self._select(self.items[0].claim_id)

    @rx.event
    async def on_load(self):
        await self._load()

    async def _select(self, claim_id: str) -> None:
        self.selected, self.refusal, self.reason = claim_id, "", ""
        self.transform, self.edited, self.previewed, self.edit_error = None, None, "", ""
        row = self.current
        if row is None:
            return
        self.edit_slot, self.edit_value = row.predicate, row.value
        try:
            payload = await self._client().lineage(row.domain, row.subject)
            self.nodes, self.edges = flow_graph(payload, selected=claim_id)
            self.column = client.column_of(payload, claim_id)
            self.entity = client.entity_of(payload)
            self.transform = client.transform_of(payload, claim_id)
            self.lineage_error = ""
        except (httpx.HTTPError, client.ContractError, ValueError) as exc:
            self.nodes, self.edges = [], []
            self.column, self.entity = None, None
            self.lineage_error = _describe(exc)

    @rx.event
    async def select(self, claim_id: str):
        await self._select(claim_id)

    @rx.event
    def set_tab(self, tab: str):
        self.tab = tab

    @rx.event
    def set_reason(self, value: str):
        self.reason = value

    @rx.event
    async def set_token(self, value: str):
        self.token = value
        await self._load()

    @rx.event
    def set_edit_slot(self, value: str):
        self.edit_slot = value

    @rx.event
    def set_edit_value(self, value: str):
        self.edit_value, self.edited, self.edit_error = value, None, ""

    @rx.event
    async def run_preview(self):
        """Run the edited reading over the selected claim's column; nothing is written."""
        self.edit_error, value = "", self.edit_value.strip()
        try:
            self.edited = await self._client().preview(self.selected, value)
            self.previewed = value
        except (httpx.HTTPError, client.ContractError, ValueError) as exc:
            self.edited, self.previewed, self.edit_error = None, "", _describe(exc)

    @rx.event
    async def accept_edited(self):
        """Propose the edited destination and reading, accept it, and reject the on-screen claim as
        superseded. Both claims stay in the record."""
        row, shown = self.current, self.edited
        if row is None:
            return
        if self.edit_slot == UNPLACED:
            self.edit_error = "Nothing is filled, so nothing is accepted; use Leave unplaced."
            return
        if shown is None or self.previewed != self.edit_value.strip():
            self.edit_error = "Preview the edited transform before accepting it."
            return
        if (self.edit_slot, shown.reading) == (row.predicate, row.value):
            self.edit_error = _UNCHANGED
            return
        curator = self._client()
        if not curator.token:
            self.edit_error = str(client.NoActor())
            return
        rows = max(shown.rows, 1)
        says = f"{shown.produced} of {shown.rows} rows give a value through {shown.chain}"
        try:
            made = await curator.propose(
                {
                    "domain": row.domain,
                    "subject": row.subject,
                    "predicate": self.edit_slot,
                    "value": shown.reading,
                    "derived_from": row.claim_id,
                    "evidence": {
                        "source": row.source_name,
                        "source_id": row.source_id,
                        "source_version": row.source_version,
                        "text": says,
                    },
                    "confidence": round(shown.produced / rows, 3),
                    "reasons": [
                        {
                            "kind": "shape match",
                            "weight": round(shown.produced / rows, 3),
                            "evidence": f"{says}.",
                        }
                    ],
                    "reasoning": f"edited by the curator from {row.predicate} = {row.value}",
                }
            )
            derived = str(made["claim_id"])
            if derived == row.claim_id:
                self.edit_error = _UNCHANGED
                return
            await curator.decide(derived, "accepted", self.reason or f"edited from {row.claim_id}")
            await curator.decide(
                row.claim_id, "rejected", f"superseded by {derived}, edited by the curator"
            )
        except (client.Refused, client.NoActor) as exc:
            self.edit_error = str(exc)
            return
        except (httpx.HTTPError, client.ContractError, ValueError) as exc:
            self.edit_error = _describe(exc)
            return
        self.notice = (
            f"accepted {self.edit_slot} = {shown.reading}; rejected {row.claim_id} as superseded"
        )
        self.items = [r for r in self.items if r.claim_id != row.claim_id]
        await self._refresh()
        await self._select(next((r.claim_id for r in self.items), ""))

    @rx.event
    async def decide(self, claim_id: str, outcome: str):
        self.notice = ""
        if claim_id != self.selected:
            self.refusal = (
                f"not sent: {claim_id} is no longer the claim on screen; "
                f"review {self.selected or 'the next claim'} before deciding"
            )
            return
        await self._decide(claim_id, outcome, self.reason)

    @rx.event
    async def leave_unplaced(self):
        """Reject the claim on screen with the reason that its column was left unplaced."""
        row = self.current
        if row is None:
            return
        self.notice, self.refusal = "", ""
        reason = f"left unplaced by the curator: {row.source_id} does not fill {row.predicate}"
        if self.reason:
            reason = f"{reason}; {self.reason}"
        await self._decide(row.claim_id, "rejected", reason)

    async def _decide(self, claim_id: str, outcome: str, reason: str) -> None:
        try:
            await self._client().decide(claim_id, outcome, reason)
        except (client.Refused, client.NoActor) as exc:
            self.refusal = str(exc)
            return
        except (httpx.HTTPError, ValueError) as exc:
            self.refusal = _describe(exc)
            return
        self.notice = f"{outcome} {claim_id}"
        self.items = [r for r in self.items if r.claim_id != claim_id]
        await self._refresh()
        nxt = next((r.claim_id for r in self.items), "")
        await self._select(nxt)


S = CurationState

_UNCHANGED = "The edit is the claim on screen, unchanged; use Accept instead."


def _alt(a: client.Alternative) -> rx.Component:
    return rx.el.li(rx.el.span(a.value, class_name="cx-mono"), " ", rx.el.span(a.confidence))


def _queue_item(r: QueueRow) -> rx.Component:
    return rx.el.button(
        rx.vstack(
            rx.hstack(
                rx.el.strong(r.triage),
                rx.el.span(r.confidence),
                rx.el.span(f"{r.domain} / {r.subject}", class_name="cx-mono"),
                spacing=tokens.GAP["hair"],
            ),
            rx.el.span(f"{r.predicate} = ", rx.el.span(r.value, class_name="cx-mono")),
            rx.el.span(r.triage_reason, class_name="cx-caption"),
            spacing=tokens.GAP["hair"],
            align_items="start",
        ),
        on_click=S.select(r.claim_id),
        class_name=rx.cond(S.selected == r.claim_id, "cx-queue-item cx-selected", "cx-queue-item"),
        aria_pressed=S.selected == r.claim_id,
    )


def queue() -> rx.Component:
    return rx.vstack(
        rx.el.h2("Review queue", class_name="cx-section"),
        rx.el.span(
            f"{S.items.length()} undecided, in the API's review order", class_name="cx-caption"
        ),
        rx.cond(S.queue_error != "", rx.el.div(S.queue_error, role="alert", class_name="cx-alert")),
        rx.foreach(S.items, _queue_item),
        spacing=tokens.GAP["row"],
        align_items="start",
        width="100%",
    )


def _decision_buttons() -> rx.Component:
    # Accept and reject at one size and weight, side by side.
    return rx.hstack(
        rx.el.button("Accept", on_click=S.decide(S.selected, "accepted"), class_name="cx-decide"),
        rx.el.button("Reject", on_click=S.decide(S.selected, "rejected"), class_name="cx-decide"),
        spacing=tokens.GAP["row"],
    )


def _notice() -> rx.Component:
    return rx.cond(S.notice != "", rx.el.div(S.notice, role="status", class_name="cx-notice"))


def _reason(r: client.ReasonRow) -> rx.Component:
    # The outcome is in words as well as style.
    return rx.el.div(
        rx.el.div(
            rx.el.span(
                rx.cond(r.check != "", r.check, r.kind),
                class_name=rx.cond(r.outcome == "passed", "cx-tag cx-tag-on", "cx-tag"),
            ),
            rx.el.span(r.weight, " · ", r.outcome, class_name="cx-caption"),
            class_name="cx-why-head",
        ),
        rx.el.p(rx.cond(r.check != "", f"{r.kind}: ", ""), r.evidence),
        class_name="cx-why",
    )


def _why(r) -> rx.Component:
    return rx.vstack(
        rx.cond(
            r.reasons.length() > 0,
            rx.fragment(
                rx.foreach(r.reasons, _reason),
                rx.el.p(
                    f"Confidence {r.confidence} is the consequence of these reasons.",
                    class_name="cx-caption",
                ),
            ),
            rx.el.p("The proposer stated no typed reasons.", class_name="cx-caption"),
        ),
        rx.cond(r.reasoning != "", rx.el.p("Proposer's note: ", r.reasoning)),
        rx.el.dl(
            rx.el.dt("Triage"),
            rx.el.dd(rx.el.strong(r.triage), " - ", r.triage_reason),
            rx.el.dt("Cites"),
            rx.el.dd(r.source, class_name="cx-mono"),
            rx.el.dt("Evidence"),
            rx.el.dd(rx.el.blockquote(r.evidence)),
            class_name="cx-facts",
        ),
        spacing=tokens.GAP["row"],
        align_items="start",
        width="100%",
    )


def _value(v: client.ValueRow) -> rx.Component:
    return rx.el.div(
        rx.el.span(v.value, class_name="cx-mono"),
        rx.el.span(v.count, rx.cond(v.note != "", f" · {v.note}", "")),
        class_name="cx-val",
    )


def _column() -> rx.Component:
    c = S.column
    return rx.cond(
        c,
        rx.vstack(
            rx.el.dl(
                rx.el.dt("Header"),
                rx.el.dd(f'"{c.header}"'),
                rx.el.dt("Address"),
                rx.el.dd(c.address, class_name="cx-mono"),
                rx.el.dt("Type"),
                rx.el.dd(c.type),
                rx.el.dt("Rows"),
                rx.el.dd(f"{c.rows}, {c.blank} blank"),
                rx.el.dt("Unique"),
                rx.el.dd(c.unique),
                class_name="cx-facts cx-kv",
            ),
            rx.el.span("Most frequent values", class_name="cx-caption"),
            rx.el.div(rx.foreach(c.top, _value), class_name="cx-vals"),
            spacing=tokens.GAP["row"],
            align_items="start",
            width="100%",
        ),
        rx.el.p("This claim was not read from a sheet column.", class_name="cx-caption"),
    )


def _example(e: client.ExampleRow) -> rx.Component:
    return rx.el.div(
        rx.el.span(e.input, class_name="cx-mono"),
        rx.el.span(" -> "),
        rx.cond(
            e.output != "",
            rx.el.strong(e.output, class_name="cx-mono"),
            rx.el.span("no value", class_name="cx-caption"),
        ),
        class_name="cx-val",
    )


def _applied(t) -> rx.Component:
    """One transform as applied: what is audited, the rows side by side, and the count."""
    return rx.vstack(
        rx.el.div(
            rx.el.strong(t.chain, class_name="cx-mono"),
            rx.el.span(t.attention, class_name=rx.cond(t.bespoke, "cx-tag cx-tag-on", "cx-tag")),
            class_name="cx-why-head",
        ),
        rx.el.p(t.why, class_name="cx-caption"),
        rx.cond(
            t.rows >= 0,
            rx.fragment(
                rx.el.span("Applied to your rows", class_name="cx-caption"),
                rx.el.div(rx.foreach(t.examples, _example), class_name="cx-vals"),
                rx.el.p(
                    rx.el.strong(f"{t.produced} of {t.rows}"),
                    " rows produce a value.",
                    rx.cond(t.complete, "", " Counted over the most frequent cells only."),
                ),
            ),
            rx.el.p(
                "The API kept no cells for this column, so it cannot be run here.",
                class_name="cx-caption",
            ),
        ),
        rx.el.p(
            rx.cond(
                t.accepted > 0,
                f"Accepted {t.accepted} time(s), on {t.accepted_on}.",
                "Never accepted before.",
            ),
            class_name="cx-caption",
        ),
        spacing=tokens.GAP["row"],
        align_items="start",
        width="100%",
    )


def _fill_option(o: FillOption) -> rx.Component:
    return rx.el.option(o.label, value=o.slot)


def _choice(c: client.ChoiceRow) -> rx.Component:
    return rx.el.button(
        c.chain,
        on_click=S.set_edit_value(c.reading),
        aria_pressed=c.reading == S.edit_value,
        class_name=rx.cond(c.reading == S.edit_value, "cx-tag cx-tag-on cx-mono", "cx-tag cx-mono"),
    )


def _transform() -> rx.Component:
    t = S.transform
    return rx.cond(
        t,
        rx.vstack(
            _applied(t),
            rx.el.p(
                "Deterministic: the same cell always gives the same answer, and you can re-run it.",
                class_name="cx-caption",
            ),
            rx.el.label("Fills", class_name="cx-caption"),
            # Debounced so the pick does not revert during the round trip.
            rx.debounce_input(
                rx.el.select(
                    rx.foreach(S.fills, _fill_option),
                    value=S.edit_slot,
                    on_change=S.set_edit_slot,
                    aria_label="Destination the transform fills",
                    class_name="cx-input",
                ),
                debounce_timeout=0,
            ),
            rx.cond(
                S.edit_slot == UNPLACED,
                rx.fragment(
                    rx.el.p(
                        "Leaving it unplaced rejects this claim: the column fills none of these "
                        "slots. Your reason is kept with the decision.",
                        class_name="cx-caption",
                    ),
                    rx.el.button(
                        "Leave unplaced", on_click=S.leave_unplaced, class_name="cx-decide"
                    ),
                ),
            ),
            rx.el.span("Transform", class_name="cx-caption"),
            rx.el.div(
                rx.foreach(t.choices, _choice),
                role="group",
                aria_label="Library tools",
                class_name="cx-vals",
            ),
            rx.el.label("Or write the reading, for a pattern(...)", class_name="cx-caption"),
            rx.debounce_input(
                rx.el.input(
                    value=S.edit_value,
                    on_change=S.set_edit_value,
                    aria_label="Column and transform chain",
                    class_name="cx-input cx-mono",
                ),
                debounce_timeout=250,
            ),
            rx.el.button("Preview", on_click=S.run_preview, class_name="cx-decide"),
            rx.cond(
                S.edited,
                rx.fragment(
                    rx.el.p(
                        "Changed from the proposal. Accepting records a new claim derived from "
                        "this one and rejects this one as superseded; both stay in the record.",
                        class_name="cx-caption",
                    ),
                    _applied(S.edited),
                    rx.el.button(
                        "Accept as edited", on_click=S.accept_edited, class_name="cx-decide"
                    ),
                ),
            ),
            rx.cond(
                S.edit_error != "", rx.el.div(S.edit_error, role="alert", class_name="cx-alert")
            ),
            spacing=tokens.GAP["row"],
            align_items="start",
            width="100%",
        ),
        rx.el.p("This claim reads no transform.", class_name="cx-caption"),
    )


def _rival(a: client.Alternative) -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.span(a.value, class_name="cx-mono"),
            rx.el.span(a.confidence),
            class_name="cx-why-head",
        ),
        rx.cond(a.reasoning != "", rx.el.p(a.reasoning, class_name="cx-caption")),
        class_name="cx-why",
    )


def _rivals(r) -> rx.Component:
    return rx.vstack(
        rx.el.div(
            rx.el.div(
                rx.el.strong(r.value, class_name="cx-mono"),
                rx.el.strong(r.confidence),
                class_name="cx-why-head",
            ),
            rx.el.p("the proposal", class_name="cx-caption"),
            class_name="cx-why",
        ),
        rx.cond(
            r.alternatives.length() > 0,
            rx.foreach(r.alternatives, _rival),
            rx.el.p("No rival was stated.", class_name="cx-caption"),
        ),
        align_items="start",
        width="100%",
    )


def _tab(name: str) -> rx.Component:
    return rx.el.button(
        name.capitalize(),
        role="tab",
        aria_selected=S.tab == name,
        on_click=S.set_tab(name),
        class_name=rx.cond(S.tab == name, "cx-tab cx-selected", "cx-tab"),
    )


def _entity() -> rx.Component:
    e = S.entity
    return rx.cond(
        e,
        rx.el.p(
            rx.el.strong(e.label),
            f": {e.filled} of {e.of} slots filled",
            class_name="cx-caption",
        ),
    )


def detail() -> rx.Component:
    r = S.current
    return rx.cond(
        r,
        rx.vstack(
            rx.el.h2(f"{r.predicate} = {r.value}", class_name="cx-section"),
            rx.el.span(
                rx.cond(r.actor_kind != "", f"{r.actor_kind} · {r.proposed_by} · ", ""),
                f"confidence {r.confidence} · awaiting review",
                class_name="cx-caption",
            ),
            rx.el.div(
                rx.el.div(
                    rx.el.div(
                        rx.cond(
                            S.lineage_error != "",
                            rx.el.div(S.lineage_error, role="alert", class_name="cx-alert"),
                            ReactFlow.create(
                                Background.create(),
                                Controls.create(show_interactive=False),
                                nodes=S.nodes,
                                edges=S.edges,
                                fit_view=True,
                                nodes_draggable=False,
                                nodes_connectable=False,
                            ),
                        ),
                        class_name="cx-flow",
                    ),
                    _entity(),
                ),
                rx.el.div(
                    rx.el.div(*[_tab(t) for t in TABS], role="tablist", class_name="cx-tabs"),
                    rx.match(
                        S.tab,
                        ("transform", _transform()),
                        ("column", _column()),
                        ("rivals", _rivals(r)),
                        _why(r),
                    ),
                    # Debounced so fast typing is not overwritten by stale server deltas. Blur
                    # flushes it before Accept/Reject.
                    rx.debounce_input(
                        rx.el.input(
                            value=S.reason,
                            on_change=S.set_reason,
                            placeholder="Reason (recorded with either outcome)",
                            aria_label="Decision reason",
                            class_name="cx-input",
                        ),
                        debounce_timeout=250,
                    ),
                    _decision_buttons(),
                    rx.cond(
                        S.refusal != "", rx.el.div(S.refusal, role="alert", class_name="cx-alert")
                    ),
                    _notice(),
                    class_name="cx-card",
                ),
                class_name="cx-review",
            ),
            spacing=tokens.GAP["row"],
            align_items="start",
            width="100%",
        ),
        rx.vstack(
            _notice(),
            rx.el.p(
                "Nothing selected. The queue is empty or has not loaded.", class_name="cx-caption"
            ),
            align_items="start",
        ),
    )


def body() -> rx.Component:
    return rx.el.div(
        rx.el.div(queue(), class_name="cx-queue"),
        rx.el.div(detail(), class_name="cx-detail"),
        class_name="cx-curation",
    )


#: Layout and the four node kinds, from the design contract's tokens only.
CSS = f"""
.cx-curation {{ display: grid; width: 100%; grid-template-columns: minmax(16rem, 1fr) 4fr;
  gap: var(--cx-space-4); }}
.cx-review {{ display: grid; width: 100%; grid-template-columns: minmax(0, 1fr) 380px;
  gap: var(--cx-space-4); align-items: start; }}
@media (max-width: 1150px) {{ .cx-review {{ grid-template-columns: minmax(0, 1fr); }} }}
.cx-card {{ display: flex; flex-direction: column; gap: var(--cx-space-3);
  border: 1px solid {design.RULE}; background: {design.SURFACE_PANEL};
  padding: var(--cx-space-3); }}
.cx-kv {{ display: grid; grid-template-columns: auto 1fr; gap: var(--cx-space-1)
  var(--cx-space-3); margin: 0; }}
.cx-kv dd {{ margin: 0; }}
@media (max-width: 800px) {{ .cx-curation {{ grid-template-columns: 1fr; }} }}
.cx-queue-item {{ width: 100%; text-align: left; padding: var(--cx-space-2); min-height: 24px;
  border: 1px solid {design.RULE}; background: {design.SURFACE_PANEL};
  color: {design.TEXT_PRIMARY}; }}
.cx-queue-item.cx-selected {{ background: {design.SURFACE_SELECTED};
  border-color: {design.RULE_STRONG}; }}
.cx-flow {{ width: 100%; height: 34rem; border: 1px solid {design.RULE};
  background: {design.SURFACE_INSET}; }}
.cx-flow .react-flow__node {{ border-radius: {design.RADIUS}; color: {design.TEXT_PRIMARY};
  background: {design.SURFACE_PANEL}; border: 1px solid {design.BORDER_CONTROL};
  font-size: {design.TEXT_DENSE}; width: 220px; text-align: left; white-space: pre-line; }}
.cx-flow .cx-flow-open {{ border-style: dashed; color: {design.TEXT_MUTED}; }}
.cx-flow .cx-flow-sheet {{ border-width: 2px; }}
.cx-tabs {{ display: flex; gap: var(--cx-space-1); border-bottom: 1px solid {design.RULE};
  width: 100%; }}
.cx-tab {{ min-height: 28px; padding: var(--cx-space-2) var(--cx-space-3); background: none;
  border: 0; border-bottom: 2px solid transparent; color: {design.TEXT_MUTED}; }}
.cx-tab.cx-selected {{ color: {design.TEXT_PRIMARY}; border-bottom-color: {design.RULE_STRONG};
  font-weight: {design.WEIGHT_STRONG}; }}
.cx-why {{ width: 100%; border-top: 1px solid {design.RULE}; padding: var(--cx-space-2) 0; }}
.cx-why-head, .cx-val {{ display: flex; justify-content: space-between;
  gap: var(--cx-space-2); }}
.cx-tag {{ border: 1px solid {design.RULE}; padding: 0 var(--cx-space-1);
  color: {design.TEXT_MUTED}; font-size: {design.TEXT_MICRO}; }}
.cx-tag-on {{ border-color: {design.RULE_STRONG}; color: {design.TEXT_PRIMARY}; }}
.cx-vals {{ width: 100%; }}
.cx-val {{ border-top: 1px solid {design.RULE}; padding: var(--cx-space-1) 0; }}
.cx-flow .cx-flow-claim {{ border-width: 2px; }}
.cx-flow .cx-flow-decision {{ border-style: dashed; }}
.cx-flow .cx-flow-selected {{ border-width: 3px; border-color: {design.RULE_STRONG};
  font-weight: {design.WEIGHT_STRONG}; }}
.cx-notice {{ border: 1px solid {design.RULE}; padding: var(--cx-space-2); }}
.cx-flow .cx-flow-entity {{ border-color: {design.RULE_STRONG}; border-width: 2px; }}
.cx-decide {{ min-width: 8rem; min-height: 28px; padding: var(--cx-space-2) var(--cx-space-4);
  border: 1px solid {design.BORDER_CONTROL}; background: {design.SURFACE_PANEL};
  color: {design.TEXT_PRIMARY}; font-weight: {design.WEIGHT_STRONG}; }}
.cx-input {{ width: 100%; min-height: 28px; border: 1px solid {design.BORDER_CONTROL};
  padding: var(--cx-space-2); }}
.cx-alert {{ border: 2px solid {design.RULE_STRONG}; padding: var(--cx-space-2);
  font-weight: {design.WEIGHT_STRONG}; }}
.cx-facts dt {{ color: {design.TEXT_MUTED}; font-size: {design.TEXT_MICRO}; }}
"""


def page() -> rx.Component:
    return rx.el.div(
        rx.vstack(
            rx.el.h1("Curation", class_name="cx-title"),
            # Debounced, as the decision reason is.
            rx.debounce_input(
                rx.el.input(
                    value=S.token,
                    on_change=S.set_token,
                    type="password",
                    placeholder="API token (required to decide: it names you to the ledger)",
                    aria_label="API token",
                    class_name="cx-input",
                ),
                debounce_timeout=250,
            ),
            body(),
            spacing=tokens.GAP["page"],
            align_items="start",
            width="100%",
        ),
        class_name="cx-root",
    )


def add_pages(app: rx.App, route: str = ROUTE) -> None:
    """Mount the review page on a host app, such as the one serving the API."""
    app.add_page(page, route=route, title="Curation", on_load=S.on_load)


def head_components() -> list[rx.Component]:
    """What a host puts in `rx.App(head_components=...)` so the page is styled."""
    return [rx.el.style(tokens.stylesheet()), rx.el.style(CSS)]


def make_app(*, api=None) -> rx.App:
    """A standalone app holding only the review page. Build once per process.

    ``api`` is a Starlette application, such as ``clinicalomx_markers.api.v1.curation_api``;
    Reflex mounts beneath it so the page and the API share one process and port.
    """
    app = rx.App(
        theme=rx.theme(**tokens.THEME),
        stylesheets=tokens.stylesheets(),
        head_components=head_components(),
        style=tokens.APP_STYLE,
        api_transformer=api,
    )
    add_pages(app, route="/")
    return app
