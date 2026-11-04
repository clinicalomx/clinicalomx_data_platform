"""A lineage payload as React Flow ``nodes`` and ``edges``. Pure data, no Reflex.

The API owns the graph. This adds positions and node text from ``kind`` and the optional
``data``: one lane per kind in `client.KINDS` order, with a sheet (``data.level == "file"``) in a
lane before its columns. Nodes in a lane stack in payload order. A claim node's id is its claim id.
"""

from __future__ import annotations

from clinicalomx.curation.client import KINDS, ContractError

COLUMN_GAP = 260
ROW_GAP = 140
#: React Flow built-in node types: a source has only an outgoing handle, an entity only an incoming
#: one. A sheet's column takes both.
_NODE_TYPE = {"source": "input", "entity": "output"}


def _weight(r: dict) -> str:
    w = r.get("weight")
    return "n/a" if w is None else f"{float(w):.2f}"


def _words(kind: str, label: str, data: dict) -> tuple[str, str]:
    """``(text, extra class)`` for one node, from figures the payload carries."""
    level = data.get("level")
    if kind == "source" and level == "file":
        return (
            f"sheet · {data.get('file', label)}\n"
            f"{data.get('sheet', '')} · {data.get('rows', 0)} rows · "
            f"{data.get('columns', 0)} columns\nhash {str(data.get('content_hash', ''))[:12]}",
            "cx-flow-sheet",
        )
    if kind == "source" and level == "column":
        head = (
            f'column · "{data.get("header", "")}"\ncol {data.get("column", "")} · '
            f"{data.get('type', '')} · {data.get('unique', 0)} unique"
        )
        if not data.get("claimed", True):
            return f"{head}\nno claim: {data.get('reason', '')}", "cx-flow-open"
        top = ", ".join(str(v.get("value", "")) for v in (data.get("top") or [])[:3])
        return f"{head}\n{top}\n{data.get('blank', 0)} blank", ""
    if kind == "claim" and data:
        lines = [f"{r.get('kind', '')} {_weight(r)}" for r in data.get("reasons") or []]
        conf = data.get("confidence")
        tail = " · ".join(
            filter(
                None,
                (
                    str(data.get("actor_kind") or ""),
                    "" if conf is None else f"{float(conf):.2f}",
                    str(data.get("outcome") or "awaiting review"),
                ),
            )
        )
        return "\n".join([label, *lines, tail]), ""
    if kind == "entity" and "slots" in data:
        lines = [
            f"{s.get('slot', '')}: {'filled' if s.get('filled') else 'open'}"
            for s in data.get("slots") or []
        ]
        return (
            "\n".join([label, *lines, f"{data.get('filled', 0)} of {data.get('of', 0)} filled"]),
            "",
        )
    return label, ""


def flow_graph(payload: dict, selected: str = "") -> tuple[list[dict], list[dict]]:
    """``(nodes, edges)`` for React Flow from a ``{nodes, edges}`` lineage payload.

    ``selected`` is the claim under review; its claim node carries ``selected`` and says so.
    """
    nodes_in = payload.get("nodes") if isinstance(payload, dict) else None
    edges_in = payload.get("edges") if isinstance(payload, dict) else None
    if not isinstance(nodes_in, list) or not isinstance(edges_in, list):
        raise ContractError("a lineage payload carries a 'nodes' list and an 'edges' list")
    rows = dict.fromkeys(range(len(KINDS) + 1), 0)
    nodes = []
    for i, n in enumerate(nodes_in):
        kind = n.get("kind") if isinstance(n, dict) else None
        if kind not in KINDS:
            raise ContractError(f"lineage node {i} has kind {kind!r}; the kinds are {KINDS}")
        if "id" not in n or "label" not in n:
            raise ContractError(f"lineage node {i} carries no 'id' or no 'label'")
        node_id = str(n["id"])
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        marked = kind == "claim" and bool(selected) and node_id == selected
        words = f"{kind} · under review · " if marked else f"{kind} · "
        text, extra = _words(kind, str(n["label"]), data)
        lane = 0 if data.get("level") == "file" else KINDS.index(kind) + 1
        classes = " ".join(filter(None, (f"cx-flow-{kind}", extra, marked and "cx-flow-selected")))
        nodes.append(
            {
                "id": node_id,
                "type": "default"
                if data.get("level") == "column"
                else _NODE_TYPE.get(kind, "default"),
                # The kind is in the text, except for a sheet and its columns.
                "data": {"label": text if data.get("level") else f"{words}{text}"},
                "position": {"x": lane * COLUMN_GAP, "y": rows[lane] * ROW_GAP},
                # Edges leave the right side and land on the left.
                "sourcePosition": "right",
                "targetPosition": "left",
                "className": classes,
                "selected": marked,
            }
        )
        rows[lane] += 1
    known = {n["id"] for n in nodes}
    edges = []
    for i, e in enumerate(edges_in):
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            raise ContractError(f"lineage edge {i} carries no 'from' or no 'to'")
        source, target = str(e["from"]), str(e["to"])
        if source not in known or target not in known:
            raise ContractError(
                f"lineage edge {i} joins {source!r} to {target!r}, "
                "and the payload has no node for one of them"
            )
        edges.append({"id": f"{source}->{target}", "source": source, "target": target})
    return nodes, edges
