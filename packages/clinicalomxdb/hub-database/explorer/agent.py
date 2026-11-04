"""The Agent tab: a chat that refines a query using the Build tab's own operations. Prototype.

The model holds one query and changes it one move at a time: choose a registry, add a
condition, list fields, list values, preview, summarise. Field and registry names it may pass
are an enum rebuilt from the current query before every call.

The query and transcript live in the page and return with each message; the server stores
neither. LangGraph runs the loop and the OpenAI SDK makes the calls, so any OpenAI-compatible
server works.

The API key arrives with the request, is never written to disk, and is scrubbed from errors.
"""

from __future__ import annotations

import copy
import json
import operator
from typing import Annotated, Any, TypedDict

import lamin_api as api

MAX_STEPS = 12

# The model never receives row values, only column names and counts. These bound what a
# summary may return.
MAX_VALUES_TO_MODEL = 60
MAX_CHARS_PER_VALUE = 120
MAX_TOOL_CHARS = 8_000

# Output token budget, sent on every call. Anthropic requires one. It is reserved from the
# context window, so it stays small for 8k local models.
MAX_TOKENS = 1024
MIN_TOKENS = 256

# Characters of field names offered as an enum, across the whole tool list.
MAX_ENUM_CHARS = 2_000
MAX_HISTORY_MESSAGES = 24


def normalise_base_url(url: str) -> str:
    """Strip whitespace and a trailing slash from a pasted base URL. Never adds /v1."""
    return (url or "").strip().rstrip("/")


# Optional call parameters, in the order they are dropped when a provider rejects one.
_OPTIONAL_PARAMS = ("temperature", "max_tokens", "max_completion_tokens")


def _adapt_params(params: dict, message: str) -> str | None:
    """Change one parameter after a provider's error. Returns what changed, or None."""
    lowered = message.lower()
    # Rename before dropping: Anthropic requires a budget.
    if "max_completion_tokens" in lowered and "max_tokens" in params:
        params["max_completion_tokens"] = params.pop("max_tokens")
        return "max_tokens -> max_completion_tokens"
    for name in _OPTIONAL_PARAMS:
        if name in params and name in lowered:
            params.pop(name)
            return f"dropped {name}"
    return None


def _is_context_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in (
        "context length", "context_length", "maximum context", "too long",
        "reduce the length", "context window"))


def _trim_history(messages: list[dict], keep: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Drop the oldest exchanges, keeping the first user message.

    The kept window never starts on a tool reply, which providers reject without its request.
    """
    if len(messages) <= keep:
        return messages
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    tail = messages[-keep:]
    while tail and (tail[0].get("role") == "tool"
                    or (tail[0].get("role") == "assistant" and tail[0].get("tool_calls"))):
        tail = tail[1:]
    if first_user is not None and first_user not in tail:
        return [first_user, {"role": "assistant",
                             "content": "[earlier steps were dropped to fit the context window]"}
                ] + tail
    return tail


def _scrub(text: str, secret: str) -> str:
    """Remove the key from text that is shown or logged."""
    if secret and len(secret) > 8:
        return text.replace(secret, "***")
    return text


def blank_query(registry: str = "Artifact") -> dict:
    return {"registry": registry, "groups": [], "match": "all",
            "feature_columns": [], "limit": api.DEFAULT_ROWS}


SYSTEM_PROMPT = """\
You help a scientist query a LaminDB instance of spatial-omics and clinical data.

The instance is a lakehouse. Entities such as Cohort, Patient, Sample, Slide and ROI are rows in
the Record registry distinguished by a type, not registries of their own. An artifact's attributes
are FEATURES stored in value tables rather than columns, and which features an artifact carries
varies from one artifact to another.

YOU ARE REFINING ONE QUERY, not writing a new one each time. add_condition narrows what is already
there. Everything you can ask about is relative to the rows matching it right now, which is what
makes the order below work:

1. add_condition to narrow.
2. available_fields to see what the MATCHED rows actually carry. Filtering by suffix .qptiff is
   what makes qptiff_study_name worth knowing about.
3. field_values before filtering on a field you have not used, so you filter on a value that
   exists rather than one you guessed. A value that matches something is not proof it is the
   only one that should: studies and cohorts often come in families like "X" and "X Sample".
   When add_condition reports other related values, decide whether they belong in the answer and
   say which you included. If they do, put them in ONE group with match "any".
4. preview when the USER wants to see rows. You will not see their values: preview returns
   column names and a count, and the rows go to the user's screen.

YOU NEVER SEE ROW CONTENTS. To learn what is in a column, call field_values for its distinct
values or summarise for a count, range or distribution. This is deliberate: some columns hold
encoded images, and a single row of those would exhaust the conversation. Ask the narrow question
instead of asking for the data.

The field names available to you change as the query narrows. That is not an error; it is the
point. If a name you expected is missing, the current rows do not carry it.

Groups are two levels: conditions within a group, groups joined by the outer match. Put
alternatives in one group with match "any" for an OR. Use the lookup "isset" to ask which rows
carry a feature at all.

Be brief. Say what you filtered and what came back. If nothing matched, say so and suggest what to
check rather than inventing an answer.
"""


# The same rules without the explanation, for when the full prompt does not fit.
SYSTEM_PROMPT_COMPACT = """\
You query a LaminDB instance. Entities are Record rows with a type; attributes are FEATURES, and
which features a row has varies.

You refine ONE query. add_condition narrows it. Everything you ask is relative to the rows
matching now.

You never see row values. preview returns column names and a count only; the rows go to the user.
Use field_values for a column's distinct values, summarise for counts and ranges.

available_fields shows what the matched rows carry. field_values before filtering on a new field.
If add_condition reports other related values of that field, decide whether they belong; if so,
put them in one group with match "any".
Groups: conditions in a group, groups joined by the outer match. match "any" for OR. Lookup
"isset" asks which rows carry a feature.

Be brief. Say what you filtered and what came back. Never invent an answer.
"""


def _available_fields(query: dict) -> tuple[list[str], list[str]]:
    """(all field paths, the features carried by the current matching rows)."""
    registry = query["registry"]
    specs = api.registry_fields(registry)
    columns = [f["path"] for f in specs if f["type"] != "feature"]
    scoped = api.features_in_scope(registry, None, query["match"], query["groups"] or None)
    if scoped is None:
        features = [f["path"] for f in specs if f["type"] == "feature" and not f.get("shadowed")]
    else:
        features = [f["name"] for f in scoped]
    return sorted(set(columns + features)), sorted(features)


def _tools(query: dict, compact: bool = False) -> list[dict]:
    """The tool schemas for this turn, with field and registry names limited to the query's."""
    registries = [r["name"] for r in api.list_registries()]
    fields, features = _available_fields(query)

    # Only add_condition carries the enum. Other tools validate the field and name the options.
    fits = (not compact
            and fields
            and sum(len(f) + 4 for f in fields) <= MAX_ENUM_CHARS)
    field_schema: dict[str, Any] = {"type": "string"}
    if fits:
        field_schema["enum"] = fields
    loose: dict[str, Any] = {
        "type": "string",
        "description": f"a field name; call available_fields for the {len(fields)} available",
    }
    if not fits:
        field_schema = dict(loose)
    feature_item: dict[str, Any] = {"type": "string"}
    if features and not compact and sum(len(f) + 4 for f in features) <= MAX_ENUM_CHARS:
        feature_item = {"type": "string", "enum": features}

    return [
        {"type": "function", "function": {
            "name": "list_registries",
            "description": "The registries and the clinicalomx entity types, with row counts.",
            "parameters": {"type": "object", "properties": {}}}},

        {"type": "function", "function": {
            "name": "set_registry",
            "description": "Choose which registry to query. CLEARS the current conditions, "
                           "because a condition names a field of the registry it was made for.",
            "parameters": {"type": "object", "properties": {
                "registry": {"type": "string", "enum": registries}},
                "required": ["registry"]}}},

        {"type": "function", "function": {
            "name": "available_fields",
            "description": "The fields you can filter on right now: the registry's columns, plus "
                           "the features the CURRENTLY MATCHED rows carry. Call after narrowing "
                           "to find out what became askable.",
            "parameters": {"type": "object", "properties": {}}}},

        {"type": "function", "function": {
            "name": "field_values",
            "description": "The distinct values a field takes ACROSS THE CURRENTLY MATCHED ROWS. "
                           "Use before filtering on a field you have not used yet, so the value "
                           "you filter on is one that exists.",
            "parameters": {"type": "object", "properties": {"field": loose},
                           "required": ["field"]}}},

        {"type": "function", "function": {
            "name": "add_condition",
            "description": "Narrow the query. Conditions in the same group are combined by that "
                           "group's match; put alternatives in one group with match 'any' for an "
                           "OR. isset and notset take no value.",
            "parameters": {"type": "object", "properties": {
                "field": field_schema,
                "lookup": {"type": "string",
                           "enum": ["exact", "contains", "icontains", "istartswith", "startswith",
                                    "in", "gt", "gte", "lt", "lte", "isnull", "isset", "notset"]},
                "value": {"description": "string or number; a list for the 'in' lookup; omitted "
                                         "for isset and notset"},
                "group": {"type": "integer",
                          "description": "Which group to add to. Omit for a new group."},
                "group_match": {"type": "string", "enum": ["all", "any"],
                                "description": "How conditions inside that group combine."}},
                "required": ["field", "lookup"]}}},

        {"type": "function", "function": {
            "name": "remove_conditions",
            "description": "Undo. Removes one group, or all of them when no group is given.",
            "parameters": {"type": "object", "properties": {
                "group": {"type": "integer"}}}}},

        {"type": "function", "function": {
            "name": "set_match",
            "description": "How the GROUPS combine with each other. Default all.",
            "parameters": {"type": "object", "properties": {
                "match": {"type": "string", "enum": ["all", "any"]}},
                "required": ["match"]}}},

        {"type": "function", "function": {
            "name": "preview",
            "description": "Run the query and return rows. Use when the rows themselves are "
                           "wanted; prefer summarise for a count or a set of values.",
            "parameters": {"type": "object", "properties": {
                "feature_columns": {
                    "type": "array",
                    "items": feature_item,
                    "description": "Feature values to show as columns."},
                "limit": {"type": "integer"}}}}},

        {"type": "function", "function": {
            "name": "summarise",
            "description": "Summarise the matched rows over the WHOLE result, not a page: count, "
                           "describe, unique, nunique, min, max, sum, mean. describe is the one "
                           "to reach for on an unfamiliar field.",
            "parameters": {"type": "object", "properties": {
                "op": {"type": "string", "enum": api.AGGREGATES},
                "target": {**loose, "description": "Field or feature; not needed for count."}},
                "required": ["op"]}}},
    ]


# Related values reported back when an exact filter is added.
MAX_NEAR_MISSES = 12


def _near_misses(registry: str, prior: dict, field: str, value: str) -> list[str]:
    """Other values of `field` that contain or are contained in `value`.

    Read from the rows matched before this condition was added.
    """
    try:
        result = api.aggregate(registry, None, field, "unique", prior["match"],
                               prior["groups"] or None)
    except Exception:
        return []
    needle = str(value).strip().lower()
    if not needle:
        return []
    related = []
    for row in result["rows"]:
        other = row[0]
        low = str(other).lower()
        if low == needle:
            continue
        if needle in low or low in needle:
            related.append(other)
    return related[:MAX_NEAR_MISSES]


def _group_index(query: dict, group: int | None, group_match: str | None) -> int:
    if group is not None and 0 <= group < len(query["groups"]):
        if group_match:
            query["groups"][group]["match"] = group_match
        return group
    query["groups"].append({"match": group_match or "all", "filters": []})
    return len(query["groups"]) - 1


def _apply(query: dict, name: str, args: dict) -> tuple[dict, dict, Any]:
    """Run one move. Returns (what the model sees, the query after it, a full result or None).

    Works on a copy, so a rejected move leaves the query unchanged.
    """
    q = copy.deepcopy(query)

    if name == "list_registries":
        return {"registries": api.list_registries(),
                "entity_types": api.list_entity_types()}, q, None

    if name == "set_registry":
        registry = args["registry"]
        api.resolve(registry)
        q = blank_query(registry)
        return {"registry": registry, "note": "conditions cleared"}, q, None

    if name == "available_fields":
        # Names only, never values.
        fields, features = _available_fields(q)
        return {"fields": fields, "features_on_matched_rows": features,
                "note": "names only; call field_values for what a column contains"}, q, None

    if name == "field_values":
        result = api.aggregate(q["registry"], None, args["field"], "unique", q["match"],
                               q["groups"] or None)
        values = [r[0] for r in result["rows"]]
        out = {"field": args["field"], "distinct": len(values),
               "values": values[:MAX_VALUES_TO_MODEL]}
        if len(values) > MAX_VALUES_TO_MODEL:
            out["note"] = (f"showing {MAX_VALUES_TO_MODEL} of {len(values)} distinct values; "
                           "narrow the query or use summarise for a count")
        return out, q, None

    if name == "add_condition":
        lookup = args["lookup"]
        condition = {"field": args["field"], "lookup": lookup,
                     "value": "" if lookup in ("isset", "notset") else args.get("value", "")}
        index = _group_index(q, args.get("group"), args.get("group_match"))
        q["groups"][index]["filters"].append(condition)
        # Building the query validates the condition.
        api.build_queryset(api.resolve(q["registry"]), q["registry"],
                           q["groups"], q["match"])
        matched = _count(q)
        out = {"query": _describe(q), "matched": matched}
        if lookup in ("exact", "in"):
            others = _near_misses(q["registry"], query, args["field"],
                                  args.get("value", ""))
            if others:
                out["other_values_of_this_field"] = others
                out["note"] = (
                    f"{len(others)} other value(s) of {args['field']} are related to "
                    f"{args.get('value')!r} and are NOT included. If they belong in the answer, "
                    "remove this condition and add them together in one group with "
                    'match "any", or use the "in" lookup with every value that applies.')
        if matched == 0:
            out["note"] = ("nothing matched. Check the value with field_values before assuming "
                           "the answer is zero." + (" " + out.get("note", "")))
        return out, q, None

    if name == "remove_conditions":
        group = args.get("group")
        if group is None:
            q["groups"] = []
        elif 0 <= group < len(q["groups"]):
            q["groups"].pop(group)
        return {"query": _describe(q), "matched": _count(q)}, q, None

    if name == "set_match":
        q["match"] = args["match"]
        return {"query": _describe(q), "matched": _count(q)}, q, None

    if name == "preview":
        if args.get("feature_columns"):
            q["feature_columns"] = args["feature_columns"]
        if args.get("limit"):
            q["limit"] = args["limit"]
        full = api.run_query(q["registry"], None, False, q["limit"], q["match"],
                             q["feature_columns"] or None, q["groups"] or None)
        # The model gets column names and a count; the rows go to the user's screen.
        seen = {"columns": full["columns"], "count": full["count"], "query": _describe(q),
                "note": "rows are displayed to the user. You cannot see their values; call "
                        "field_values or summarise if you need to know what is in a column."}
        return seen, q, full

    if name == "summarise":
        full = api.aggregate(q["registry"], None, args.get("target"), args["op"], q["match"],
                             q["groups"] or None)
        return {"columns": full["columns"], "rows": full["rows"][:MAX_VALUES_TO_MODEL],
                "query": _describe(q)}, q, full

    return {"error": f"unknown tool {name!r}"}, q, None


def _shrink(value):
    """Truncate oversized strings and lists on their way to the model."""
    if isinstance(value, str):
        if len(value) <= MAX_CHARS_PER_VALUE:
            return value
        return f"{value[:MAX_CHARS_PER_VALUE]}... [{len(value)} chars, truncated]"
    if isinstance(value, list):
        clipped = [_shrink(v) for v in value[:MAX_VALUES_TO_MODEL]]
        if len(value) > MAX_VALUES_TO_MODEL:
            clipped.append(f"... {len(value) - MAX_VALUES_TO_MODEL} more")
        return clipped
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    return value


def _as_tool_content(payload) -> str:
    """Serialise a tool result as JSON, capped in length."""
    text = json.dumps(_shrink(payload), default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + ' ... [truncated; ask for a narrower summary]"}'
    return text


def _describe(query: dict) -> str:
    """The query in one line."""
    if not query["groups"]:
        return f"{query['registry']}: no conditions"
    joiner = " OR " if query["match"] == "any" else " AND "
    parts = []
    for g in query["groups"]:
        inner = (" OR " if g["match"] == "any" else " AND ").join(
            f"{f['field']} {f['lookup']} {f['value']!r}" for f in g["filters"])
        parts.append(f"({inner})" if len(g["filters"]) > 1 else inner)
    return f"{query['registry']}: " + joiner.join(parts)


def _count(query: dict) -> int | str:
    """How many rows the query matches now."""
    try:
        return int(api.aggregate(query["registry"], None, None, "count", query["match"],
                                 query["groups"] or None)["rows"][0][0])
    except Exception as e:
        return f"unknown: {type(e).__name__}"


class Turn(TypedDict):
    messages: Annotated[list, operator.add]
    steps: int
    query: dict
    result: Any


def build_graph(client, model: str, secret: str = "", emit=lambda event: None):
    """The agent loop. `emit` receives an event per node entry, tool call and tool result."""
    from langgraph.graph import END, StateGraph

    # Adjusted when a provider rejects a parameter, and kept for the rest of the turn.
    params: dict[str, Any] = {"temperature": 0, "max_tokens": MAX_TOKENS}
    fit = {"compact": False, "trim": False}

    def call_model(state: Turn) -> dict:
        emit({"type": "enter", "node": "model", "step": state["steps"] + 1,
              "query": _describe(state["query"])})
        response = None
        # When the prompt does not fit, give up in order: output budget, field enums, history.
        for _ in range(6):
            messages = _trim_history(state["messages"]) if fit["trim"] else state["messages"]
            prompt = SYSTEM_PROMPT_COMPACT if fit["compact"] else SYSTEM_PROMPT
            request = {
                "model": model,
                "messages": [{"role": "system", "content": prompt}] + messages,
                "tools": _tools(state["query"], compact=fit["compact"]),
            }
            try:
                response = client.chat.completions.create(**request, **params)
                break
            except Exception as e:
                text = str(e)
                if _is_context_error(text):
                    if params.get("max_tokens", 0) > MIN_TOKENS:
                        params["max_tokens"] = max(MIN_TOKENS, params["max_tokens"] // 2)
                        emit({"type": "fit",
                              "action": f"reduced the output budget to {params['max_tokens']}"})
                        continue
                    if not fit["compact"]:
                        fit["compact"] = True
                        emit({"type": "fit",
                              "action": "shortened the instructions and dropped the field "
                                        "lists from the tool schema"})
                        continue
                    if not fit["trim"]:
                        fit["trim"] = True
                        emit({"type": "fit", "action": "dropped older steps"})
                        continue
                    raise RuntimeError(
                        "the prompt does not fit this model's context even after reducing the "
                        "output budget, shortening the instructions, dropping the field lists "
                        "and trimming the history. Use a model with a larger context window, or "
                        "clear the conversation and ask a narrower question. The provider said: "
                        + _scrub(text, secret)) from None
                if _adapt_params(params, text) is not None:
                    continue
                raise RuntimeError(_scrub(f"{type(e).__name__}: {e}", secret)) from None
        if response is None:
            raise RuntimeError(
                "the prompt does not fit this model's context even after reducing the output "
                "budget, the tool schema and the history. Use a model with a larger window, or "
                "clear the conversation and ask a narrower question.")
        message = response.choices[0].message
        # None, not "": Anthropic rejects an empty text block when history is replayed.
        out: dict = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            out["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in message.tool_calls
            ]
        elif out["content"] is None:
            out["content"] = ""
        emit({"type": "exit", "node": "model",
              "tool_calls": [c["function"]["name"] for c in out.get("tool_calls", [])],
              "content": out["content"] or None})
        return {"messages": [out], "steps": state["steps"] + 1}

    def call_tools(state: Turn) -> dict:
        emit({"type": "enter", "node": "tools",
              "calls": [c["function"]["name"]
                        for c in state["messages"][-1].get("tool_calls", [])]})
        replies = []
        query, result = state["query"], state.get("result")
        for call in state["messages"][-1].get("tool_calls", []):
            name = call["function"]["name"]
            args = {}
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                emit({"type": "tool_call", "name": name, "arguments": args})
                seen, query, rendered = _apply(query, name, args)
                if rendered is not None:
                    result = rendered
                emit({"type": "tool_result", "name": name, "ok": True,
                      "summary": _as_tool_content(seen),
                      "query": _describe(query)})
            except Exception as e:
                emit({"type": "tool_result", "name": name, "ok": False,
                      "summary": f"{type(e).__name__}: {e}", "query": _describe(query)})
                # Returned to the model, not raised, so it can correct itself.
                seen = {"error": f"{type(e).__name__}: {e}"}
            replies.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                            "content": _as_tool_content(seen)})
        emit({"type": "exit", "node": "tools", "query": _describe(query)})
        return {"messages": replies, "query": query, "result": result}

    def next_step(state: Turn) -> str:
        if state["steps"] >= MAX_STEPS:
            emit({"type": "edge", "to": "end", "reason": f"step limit ({MAX_STEPS}) reached"})
            return END
        if state["messages"][-1].get("tool_calls"):
            emit({"type": "edge", "to": "tools", "reason": "the model asked for tools"})
            return "tools"
        emit({"type": "edge", "to": "end", "reason": "no more tool calls"})
        return END

    graph = StateGraph(Turn)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    graph.set_entry_point("model")
    graph.add_conditional_edges("model", next_step, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph.compile()


GRAPH_SHAPE = {
    "nodes": ["start", "model", "tools", "end"],
    "edges": [["start", "model"], ["model", "tools"], ["tools", "model"], ["model", "end"]],
}


def run_stream(messages: list[dict], base_url: str, api_key: str, model: str,
               query: dict | None = None):
    """The same turn as run(), yielding events as they happen.

    The graph runs on a worker thread and pushes events onto a queue this generator drains.
    """
    import queue
    import threading

    events: queue.Queue = queue.Queue()
    outcome: dict = {}

    def worker():
        try:
            outcome["result"] = _invoke(messages, base_url, api_key, model, query, events.put)
        except Exception as e:
            outcome["error"] = str(e)
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield {"type": "graph", **GRAPH_SHAPE}
    while True:
        event = events.get()
        if event is None:
            break
        yield event
    thread.join(timeout=5)
    if "error" in outcome:
        yield {"type": "error", "message": outcome["error"]}
    else:
        yield {"type": "done", **outcome["result"]}


def _invoke(messages, base_url, api_key, model, query, emit) -> dict:
    from openai import OpenAI

    base_url = normalise_base_url(base_url)
    if not base_url or not model:
        raise ValueError("a base URL and a model are required")
    client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=120.0)
    final = build_graph(client, model, api_key, emit).invoke(
        {"messages": messages, "steps": 0, "query": query or blank_query(), "result": None},
        {"recursion_limit": MAX_STEPS * 2 + 4},
    )
    # The table shows the final query, run once at the end.
    result = final.get("result")
    query = final["query"]
    if query["groups"]:
        try:
            result = api.run_query(query["registry"], None, False, query["limit"],
                                   query["match"], query["feature_columns"] or None,
                                   query["groups"])
        except Exception:
            # A broken final query keeps the last successful table and the chat answer.
            pass
    return {
        "messages": final["messages"][len(messages):],
        "query": query,
        "summary": _describe(query),
        "result": result,
    }


def run(messages: list[dict], base_url: str, api_key: str, model: str,
        query: dict | None = None) -> dict:
    """One turn, continuing from the query the previous turn left behind."""
    return _invoke(messages, base_url, api_key, model, query, lambda event: None)
