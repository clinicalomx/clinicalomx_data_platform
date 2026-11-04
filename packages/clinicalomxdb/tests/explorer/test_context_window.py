"""Fitting a small local model's context window, and showing the final query's result.

The fake provider refuses requests over a fixed window, as a local server does.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/cwstore", name="cw")
import lamindb as ln

# Long feature names, so the enum is the largest part of the schema.
for i in range(60):
    ln.Feature(name=f"qptiff_acquisition_parameter_number_{i:02d}", dtype=str).save()
ln.Feature(name="study", dtype=str).save()
for key, study in [("img/a.qptiff", "STUDY-A"), ("img/b.qptiff", "STUDY-B"),
                   ("img/c.qptiff", "STUDY-A")]:
    p = "/tmp/" + key.replace("/", "_"); open(p, "w").write("payload " + key)
    ln.Artifact(p, key=key).save().features.add_values({"study": study})

sys.path.insert(0, "/explorer")
import agent

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r} (want {want!r})")
    if not ok: fails.append(label)

# Mutable, so one provider can play a large or small window.
WINDOW = [8192]
CHARS_PER_TOKEN = 4
SCRIPT, SEEN = [], []


class Window(BaseHTTPRequestHandler):
    """Refuses when reservation plus prompt exceeds a fixed window."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        prompt = len(json.dumps(body["messages"]) + json.dumps(body.get("tools", "")))
        tokens = prompt // CHARS_PER_TOKEN
        reserve = body.get("max_tokens", 0)
        if tokens + reserve > WINDOW[0]:
            msg = (f"This model's maximum context length is {WINDOW[0]} tokens. However, you "
                   f"requested {reserve} output tokens and your prompt contains at least "
                   f"{tokens} input tokens, for a total of at least {tokens + reserve} tokens.")
            payload = json.dumps({"error": {"message": msg, "type": "BadRequestError",
                                            "code": 400}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
            return
        reply = SCRIPT.pop(0) if SCRIPT else {"content": "done"}
        message = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            message["tool_calls"] = [
                {"id": f"c{len(SEEN)}_{i}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a)}}
                for i, (n, a) in enumerate(reply["tool_calls"])]
        payload = json.dumps({"id": "x", "object": "chat.completion", "created": 0,
                              "model": "fake",
                              "choices": [{"index": 0, "message": message,
                                           "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)


server = HTTPServer(("127.0.0.1", 0), Window)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/v1"

# --- the output reservation suits a small window --------------------------------------------
eq("the default output reservation suits a small window", agent.MAX_TOKENS, 1024)

# --- the enum is carried once ------------------------------------------------------------------
tools = agent._tools(agent.blank_query("Artifact"))
with_enum = [t["function"]["name"] for t in tools
             if '"enum"' in json.dumps(t["function"]["parameters"].get("properties", {}))
             and "field" in json.dumps(t["function"]["parameters"].get("properties", {}))]
eq("only add_condition constrains the field name by enum",
   [n for n in with_enum if n in ("add_condition", "field_values", "summarise")],
   ["add_condition"])

compact = agent._tools(agent.blank_query("Artifact"), compact=True)
# Compaction keeps the small enums on add_condition and drops the field list.
field_prop = lambda ts: [t for t in ts if t["function"]["name"] == "add_condition"][0][
    "function"]["parameters"]["properties"]["field"]
eq("normally the field name is constrained by enum", "enum" in field_prop(tools), True)
eq("compacting drops the field enum", "enum" in field_prop(compact), False)
eq("compacting keeps the lookup enum, which is small and load-bearing",
   "enum" in [t for t in compact if t["function"]["name"] == "add_condition"][0][
       "function"]["parameters"]["properties"]["lookup"], True)
eq("compacting is materially smaller",
   len(json.dumps(compact)) < len(json.dumps(tools)), True)

# --- the ladder, against a window that actually refuses ----------------------------------------
def turn(window, script, question="how many qptiff artifacts"):
    WINDOW[0] = window
    SCRIPT.clear(); SEEN.clear(); SCRIPT.extend(script)
    return agent.run([{"role": "user", "content": question}], BASE, "not-needed", "fake-model")

SCRIPT_ONE = [
    {"tool_calls": [("add_condition", {"field": "key", "lookup": "icontains",
                                       "value": ".qptiff"})]},
    {"content": "3 qptiff artifacts."},
]

# Roomy: nothing should be given up.
out = turn(8192, list(SCRIPT_ONE))
eq("with room, the turn runs untouched", out["messages"][-1]["content"], "3 qptiff artifacts.")
eq("and nothing was conceded",
   {b.get("max_tokens") for b in SEEN}, {agent.MAX_TOKENS})

# Measured overhead: about 1,500 tokens full, 1,100 compacted. 2400 needs a smaller
# reservation; 1800 also needs compaction.
out = turn(2400, list(SCRIPT_ONE))
eq("cramped, the turn still completes", out["messages"][-1]["content"], "3 qptiff artifacts.")
budgets = [b.get("max_tokens") for b in SEEN]
eq("the reservation was reduced", budgets[-1] < agent.MAX_TOKENS, True)
eq("it never went below the floor", min(budgets) >= agent.MIN_TOKENS, True)
eq("the schema was NOT sacrificed while the budget could still give",
   any('"enum"' in json.dumps(b.get("tools", "")) for b in SEEN), True)

# Smaller still: the field enums go too.
out = turn(1800, list(SCRIPT_ONE))
eq("tighter, the turn still completes", out["messages"][-1]["content"], "3 qptiff artifacts.")
last_field = [t for t in SEEN[-1]["tools"] if t["function"]["name"] == "add_condition"][0][
    "function"]["parameters"]["properties"]["field"]
eq("the field enum was dropped to fit", "enum" in last_field, False)
eq("but only after the budget had been reduced", SEEN[-1]["max_tokens"], agent.MIN_TOKENS)
eq("the instructions were shortened along with the schema",
   len(SEEN[-1]["messages"][0]["content"]) < len(agent.SYSTEM_PROMPT), True)

# Below every concession, the error says so.
try:
    turn(400, list(SCRIPT_ONE))
    fails.append("impossible window"); print("FAIL impossible window: no error")
except Exception as e:
    eq("an unfittable window is reported plainly", "does not fit" in str(e), True)

WINDOW[0] = 8192

# --- history trimming keeps the conversation valid ----------------------------------------------
history = [{"role": "user", "content": "first question"}]
for i in range(30):
    history.append({"role": "assistant", "content": None,
                    "tool_calls": [{"id": f"c{i}", "type": "function",
                                    "function": {"name": "list_registries", "arguments": "{}"}}]})
    history.append({"role": "tool", "tool_call_id": f"c{i}", "name": "list_registries",
                    "content": "{}"})
trimmed = agent._trim_history(history, keep=8)
eq("trimming shortens the history", len(trimmed) < len(history), True)
eq("it never begins on an orphaned tool reply", trimmed[0].get("role") != "tool", True)
eq("the original question is kept", trimmed[0]["content"], "first question")
for i, m in enumerate(trimmed):
    if m.get("role") == "tool":
        prior = trimmed[i - 1]
        if not (prior.get("role") == "assistant" and prior.get("tool_calls")):
            fails.append("orphaned tool reply"); print("FAIL orphaned tool reply at", i)
print("OK   every kept tool reply still follows its request")

# --- the table shows the FINAL query, not a mid-turn preview -----------------------------------
SCRIPT.clear(); SEEN.clear()
SCRIPT.extend([
    {"tool_calls": [("add_condition", {"field": "key", "lookup": "icontains",
                                       "value": ".qptiff"})]},
    # A preview of the wider query, a narrowing, then an answer. The table follows the narrowing.
    {"tool_calls": [("preview", {})]},
    {"tool_calls": [("add_condition", {"field": "study", "lookup": "exact",
                                       "value": "STUDY-A"})]},
    {"content": "Two are STUDY-A."},
])
out = agent.run([{"role": "user", "content": "qptiff, then only STUDY-A"}],
                BASE, "not-needed", "fake-model")
eq("the result matches the final query, not the mid-turn preview", out["result"]["count"], 2)
eq("the summary agrees with it", "STUDY-A" in out["summary"], True)

# An answer with no query shows no table.
SCRIPT.clear(); SEEN.clear()
SCRIPT.extend([{"tool_calls": [("list_registries", {})]}, {"content": "Artifacts and records."}])
out = agent.run([{"role": "user", "content": "what is in here"}], BASE, "not-needed", "fake-model")
eq("no conditions means no table", out["result"], None)

server.shutdown()
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
