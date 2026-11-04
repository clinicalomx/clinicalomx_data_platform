"""The Agent tab's loop, driven by a fake OpenAI-compatible server. No API key, no network.

Checks tool dispatch, tool errors returned to the model, loop termination, and the full
result reaching the page. Whether a real model chooses good arguments is not tested.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/agentstore", name="agt")
import lamindb as ln

ln.Feature(name="qptiff_study_name", dtype=str).save()
ln.Feature(name="qptiff_scanner", dtype=str).save()
ln.Feature(name="rnaseq_kit", dtype=str).save()
# Two disjoint feature vocabularies, so the available fields change after narrowing.
for key, values in [
    ("img/a.qptiff", {"qptiff_study_name": "STUDY-A", "qptiff_scanner": "Vectra"}),
    ("img/b.qptiff", {"qptiff_study_name": "STUDY-B", "qptiff_scanner": "PhenoImager"}),
    ("img/c.qptiff", {"qptiff_study_name": "STUDY-A", "qptiff_scanner": "Vectra"}),
    ("rna/x.h5ad", {"rnaseq_kit": "KitX"}),
]:
    path = "/tmp/" + key.replace("/", "_")
    open(path, "w").write("payload " + key)
    ln.Artifact(path, key=key).save().features.add_values(values)

sys.path.insert(0, "/explorer")
import agent

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r} (want {want!r})")
    if not ok: fails.append(label)


# --- a stand-in for the model ------------------------------------------------------------------
SCRIPT = []          # queue of replies the fake model will give, in order
REJECT = set()       # parameter names this fake provider refuses, to test adaptation
SEEN = []            # every request body it received, so the test can inspect the conversation


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        # Reject a parameter with a 400, as a real provider does.
        for name in REJECT:
            if name in body:
                error = json.dumps({"error": {
                    "code": "invalid_request_error", "type": "invalid_request_error",
                    "message": f"`{name}` is deprecated for this model."}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error)))
                self.end_headers()
                self.wfile.write(error)
                return
        reply = SCRIPT.pop(0) if SCRIPT else {"content": "done", "tool_calls": None}
        message = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            message["tool_calls"] = [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(reply["tool_calls"])
            ]
        payload = json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = HTTPServer(("127.0.0.1", 0), FakeModel)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/v1"

def ask(question, script):
    SCRIPT.clear(); SCRIPT.extend(script)
    SEEN.clear()
    return agent.run([{"role": "user", "content": question}], BASE, "not-needed", "fake-model")


# --- the argument space narrows as the query narrows ------------------------------------------
# The fields the model may name are recomputed from the query.
def field_enum(tools):
    add = [t for t in tools if t["function"]["name"] == "add_condition"][0]
    return add["function"]["parameters"]["properties"]["field"].get("enum") or []

wide = agent._tools(agent.blank_query("Artifact"))
eq("before narrowing, every feature is offerable",
   {"qptiff_study_name", "rnaseq_kit"} <= set(field_enum(wide)), True)

narrowed = agent.blank_query("Artifact")
narrowed["groups"] = [{"match": "all", "filters": [
    {"field": "key", "lookup": "icontains", "value": ".qptiff"}]}]
tight = agent._tools(narrowed)
eq("after narrowing, the qptiff features remain",
   {"qptiff_study_name", "qptiff_scanner"} <= set(field_enum(tight)), True)
eq("after narrowing, an unrelated feature is NOT offerable",
   "rnaseq_kit" in field_enum(tight), False)
eq("columns are always offerable", "key" in field_enum(tight), True)
eq("registries are an enum too",
   "Artifact" in [t for t in wide if t["function"]["name"] == "set_registry"][0]
   ["function"]["parameters"]["properties"]["registry"]["enum"], True)

# --- the moves, applied to a query rather than composed from scratch --------------------------
q = agent.blank_query("Artifact")
seen, q, _ = agent._apply(q, "add_condition",
                          {"field": "key", "lookup": "icontains", "value": ".qptiff"})
eq("a condition reports the match count", seen["matched"], 3)
eq("the query is summarised back", "key icontains" in seen["query"], True)

seen, q, _ = agent._apply(q, "field_values", {"field": "qptiff_study_name"})
eq("values are constrained to the matched rows", sorted(seen["values"]), ["STUDY-A", "STUDY-B"])

seen, q, _ = agent._apply(q, "add_condition",
                          {"field": "qptiff_study_name", "lookup": "exact", "value": "STUDY-A"})
eq("refinement narrows further", seen["matched"], 2)

seen, q, _ = agent._apply(q, "field_values", {"field": "qptiff_scanner"})
eq("the third question is constrained by the first two", seen["values"], ["Vectra"])

seen, q, full = agent._apply(q, "preview", {"feature_columns": ["qptiff_study_name"]})
eq("preview returns the refined rows", full["count"], 2)
eq("preview merged the requested feature", "qptiff_study_name" in full["columns"], True)

seen, q, _ = agent._apply(q, "available_fields", {})
eq("available_fields reflects the refinement",
   "rnaseq_kit" in seen["fields"], False)

# an OR group, and undo
q2 = agent.blank_query("Artifact")
_, q2, _ = agent._apply(q2, "add_condition",
                        {"field": "key", "lookup": "icontains", "value": ".qptiff"})
_, q2, _ = agent._apply(q2, "add_condition", {"field": "qptiff_study_name", "lookup": "exact",
                                              "value": "STUDY-A", "group": 1,
                                              "group_match": "any"})
seen, q2, _ = agent._apply(q2, "add_condition", {"field": "qptiff_study_name", "lookup": "exact",
                                                 "value": "STUDY-B", "group": 1})
eq("an OR group inside an AND", seen["matched"], 3)
seen, q2, _ = agent._apply(q2, "remove_conditions", {"group": 1})
eq("undo restores the wider result", seen["matched"], 3)
seen, q2, _ = agent._apply(q2, "remove_conditions", {})
eq("clearing removes everything", seen["matched"], 4)

# a rejected condition must not half-apply
q3 = agent.blank_query("Artifact")
_, q3, _ = agent._apply(q3, "add_condition",
                        {"field": "key", "lookup": "icontains", "value": ".qptiff"})
before = json.dumps(q3)
try:
    agent._apply(q3, "add_condition", {"field": "not_a_field", "lookup": "exact", "value": "x"})
    fails.append("bad field rejected"); print("FAIL bad field rejected: not raised")
except Exception as e:
    eq("a bad field is refused", "not a filterable field" in str(e), True)
eq("the query is unchanged after a refusal", json.dumps(q3), before)

# --- the loop, end to end through the graph ---------------------------------------------------
out = ask("how many qptiff artifacts are there", [
    {"tool_calls": [("add_condition", {"field": "key", "lookup": "icontains",
                                       "value": ".qptiff"})]},
    {"tool_calls": [("summarise", {"op": "count"})]},
    {"content": "There are 3."},
])
eq("final answer reaches the caller", out["messages"][-1]["content"], "There are 3.")
counted = [m for m in out["messages"] if m.get("name") == "summarise"][0]
eq("count ran against the REFINED query", json.loads(counted["content"])["rows"], [["3"]])
eq("the refined query comes back for the next turn",
   out["query"]["groups"][0]["filters"][0]["field"], "key")
eq("a summary of it comes back too", "key icontains" in out["summary"], True)

eq("model saw the tool result",
   any(m.get("role") == "tool" for m in SEEN[-1]["messages"]), True)
eq("system prompt is sent", SEEN[0]["messages"][0]["role"], "system")
eq("the builder's moves are all advertised",
   sorted(t["function"]["name"] for t in SEEN[0]["tools"]),
   ["add_condition", "available_fields", "field_values", "list_registries", "preview",
    "remove_conditions", "set_match", "set_registry", "summarise"])

# The enum widens and narrows within one turn as the query changes.
before_enum = set(field_enum(SEEN[0]["tools"]))
after_enum = set(field_enum(SEEN[-1]["tools"]))
eq("the enum narrowed mid-turn as the query narrowed",
   ("rnaseq_kit" in before_enum, "rnaseq_kit" in after_enum), (True, False))

# --- a second turn continues from where the first stopped -------------------------------------
SCRIPT.clear(); SCRIPT.extend([
    {"tool_calls": [("add_condition", {"field": "qptiff_study_name", "lookup": "exact",
                                       "value": "STUDY-A"})]},
    {"tool_calls": [("preview", {})]},
    {"content": "Two of them are STUDY-A."},
])
SEEN.clear()
second = agent.run(
    [{"role": "user", "content": "now only STUDY-A"}], BASE, "not-needed", "fake-model",
    out["query"])
eq("the second turn refined rather than restarted", second["result"]["count"], 2)
eq("both conditions are in the carried query", len(second["query"]["groups"]), 2)

# --- rows reach the page in full while the model sees a page ----------------------------------
out = ask("list them", [
    {"tool_calls": [("preview", {"feature_columns": ["qptiff_study_name"]})]},
    {"content": "Here they are."},
])
eq("full result returned for the page", out["result"]["count"], 4)
eq("merged feature column present", "qptiff_study_name" in out["result"]["columns"], True)

# --- errors come back as values the model can act on ------------------------------------------
out = ask("filter by something that does not exist", [
    {"tool_calls": [("add_condition", {"field": "not_a_field", "lookup": "exact", "value": "x"})]},
    {"content": "That field does not exist; I will check what does."},
])
reply = [m for m in out["messages"] if m["role"] == "tool"][0]
eq("field error handed back as a value",
   "not a filterable field" in json.loads(reply["content"]).get("error", ""), True)
eq("turn continued after the error",
   out["messages"][-1]["content"], "That field does not exist; I will check what does.")

for label, script in [
    ("unknown tool", [{"tool_calls": [("no_such_tool", {})]}, {"content": "ok"}]),
    ("unknown registry", [{"tool_calls": [("set_registry", {"registry": "Nope"})]},
                          {"content": "ok"}]),
    ("bad lookup", [{"tool_calls": [("add_condition", {"field": "key", "lookup": "nope",
                                                       "value": "x"})]}, {"content": "ok"}]),
]:
    out = ask("try it", script)
    eq(f"{label} did not crash the turn", out["messages"][-1]["content"], "ok")

# --- a model that will not stop ----------------------------------------------------------------
out = ask("go forever", [{"tool_calls": [("list_registries", {})]} for _ in range(50)])
eq("runaway loop is bounded",
   len([m for m in out["messages"] if m["role"] == "tool"]) <= agent.MAX_STEPS, True)

# --- credentials --------------------------------------------------------------------------------
try:
    agent.run([{"role": "user", "content": "hi"}], "", "k", "m")
    fails.append("missing base url"); print("FAIL missing base url: not rejected")
except ValueError as e:
    eq("missing base url rejected", "base URL" in str(e), True)

eq("the api key never appears in what is sent to the model",
   "not-needed" in json.dumps(SEEN), False)

# --- the wire format Anthropic's compatibility layer requires ---------------------------------
# OpenAI accepts both shapes; Anthropic rejects them.
out = ask("two turns so the history is replayed", [
    {"tool_calls": [("list_registries", {})]},
    {"content": "done"},
])
eq("max_tokens is sent (Anthropic requires a token budget)",
   all(body.get("max_tokens") == agent.MAX_TOKENS for body in SEEN), True)
replayed = [m for m in SEEN[-1]["messages"]
            if m.get("role") == "assistant" and m.get("tool_calls")]
eq("an assistant message with tool calls has null content, not empty string",
   [m.get("content") for m in replayed], [None])
eq("a plain assistant answer carries a string", out["messages"][-1]["content"], "done")
eq("temperature is pinned", SEEN[0].get("temperature"), 0)

for raw, want in [
    ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1"),
    ("  https://api.anthropic.com/v1  ", "https://api.anthropic.com/v1"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
    ("http://localhost:11434", "http://localhost:11434"),
]:
    eq(f"base url {raw!r}", agent.normalise_base_url(raw), want)

eq("the key is scrubbed from errors",
   agent._scrub("401 from https://api.anthropic.com with key sk-ant-secret-value",
                "sk-ant-secret-value"),
   "401 from https://api.anthropic.com with key ***")

# --- a provider that rejects a call parameter --------------------------------------------------
# A rejected parameter is dropped and the call retried, not surfaced as a failed turn.
REJECT.add("temperature")
out = ask("still works without temperature", [
    {"tool_calls": [("summarise", {"op": "count"})]},
    {"content": "4 artifacts."},
])
eq("a rejected temperature is dropped and the turn completes",
   out["messages"][-1]["content"], "4 artifacts.")
# SEEN[0] is the rejected attempt. The adaptation persists for the rest of the turn.
eq("the first attempt carried temperature", "temperature" in SEEN[0], True)
eq("every call after the rejection omitted it",
   [("temperature" in b) for b in SEEN[1:]], [False] * (len(SEEN) - 1))
eq("more than one call was made, so persistence is actually covered", len(SEEN) >= 3, True)
eq("the token budget survives the drop",
   all(b.get("max_tokens") == agent.MAX_TOKENS for b in SEEN), True)
REJECT.clear()

# A provider wanting the newer budget name gets a rename, not a drop: Anthropic requires one.
params = {"temperature": 0, "max_tokens": 4096}
eq("max_tokens is renamed when asked for max_completion_tokens",
   (agent._adapt_params(params, "Use `max_completion_tokens` instead of `max_tokens`."), params),
   ("max_tokens -> max_completion_tokens", {"temperature": 0, "max_completion_tokens": 4096}))

params = {"temperature": 0, "max_tokens": 4096}
eq("an unrelated error changes nothing",
   (agent._adapt_params(params, "model not found"), params),
   (None, {"temperature": 0, "max_tokens": 4096}))

# An error that is not about a parameter must still reach the user.
REJECT.add("model")
try:
    ask("unrecoverable", [{"content": "never reached"}])
    fails.append("unrecoverable error"); print("FAIL unrecoverable error: not raised")
except Exception as e:
    eq("an error naming nothing adaptable is surfaced", "model" in str(e).lower(), True)
REJECT.clear()


# --- the event stream behind the graph view ---------------------------------------------------
SCRIPT.clear(); SEEN.clear()
SCRIPT.extend([
    {"tool_calls": [("add_condition", {"field": "key", "lookup": "icontains",
                                       "value": ".qptiff"})]},
    {"tool_calls": [("summarise", {"op": "count"})]},
    {"content": "3 of them."},
])
events = list(agent.run_stream([{"role": "user", "content": "how many"}],
                               BASE, "not-needed", "fake-model"))
kinds = [e["type"] for e in events]

eq("the shape of the graph comes first", kinds[0], "graph")
eq("the graph names its nodes",
   events[0]["nodes"], ["start", "model", "tools", "end"])
eq("the stream ends with a result", kinds[-1], "done")

# Entering a node is reported before the work.
first_enter = next(e for e in events if e["type"] == "enter")
eq("entering the model is reported before anything it does",
   (first_enter["node"], kinds.index("enter") < kinds.index("exit")), ("model", True))

eq("both nodes are entered",
   sorted({e["node"] for e in events if e["type"] == "enter"}), ["model", "tools"])
eq("the model is entered once per step",
   len([e for e in events if e["type"] == "enter" and e["node"] == "model"]), 3)

calls = [e for e in events if e["type"] == "tool_call"]
eq("each tool call is reported with its arguments",
   [(c["name"], c["arguments"].get("field") or c["arguments"].get("op")) for c in calls],
   [("add_condition", "key"), ("summarise", "count")])

results = [e for e in events if e["type"] == "tool_result"]
eq("each result is reported", [r["ok"] for r in results], [True, True])
eq("the result shown is the text the model was given", "matched" in results[0]["summary"], True)
eq("the running query is reported alongside",
   "key icontains" in results[0]["query"], True)

edges = [e for e in events if e["type"] == "edge"]
eq("edge decisions are reported with a reason",
   [(e["to"], "tool" in e["reason"] or "no more" in e["reason"]) for e in edges],
   [("tools", True), ("tools", True), ("end", True)])

eq("the final answer arrives on the stream", events[-1]["messages"][-1]["content"], "3 of them.")
eq("streaming and blocking agree on the query",
   events[-1]["summary"], "Artifact: key icontains '.qptiff'")

# A failure ends the stream as an event.
REJECT.add("model")
bad = list(agent.run_stream([{"role": "user", "content": "x"}], BASE, "k", "fake-model"))
eq("a failure ends the stream with an error event", bad[-1]["type"], "error")
REJECT.clear()

# The stream is bounded in size like the context.
biggest = max(len(json.dumps(e, default=str)) for e in events)
eq("no single event is large", biggest < agent.MAX_TOOL_CHARS + 500, True)
eq("the whole stream is small",
   sum(len(json.dumps(e, default=str)) for e in events) < 20_000, True)

server.shutdown()
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
