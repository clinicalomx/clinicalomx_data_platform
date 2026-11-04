"""Nothing sent to the model scales with the data.

The model gets column names and counts, never row values; tools that return values are
bounded. Fixture rows each hold an encoded image.
"""
import base64
import struct
import sys
import zlib

import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/ctxstore", name="ctx")
import lamindb as ln


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


THUMB = base64.b64encode(png(256, 256, (200, 40, 40))).decode()
ln.Feature(name="qptiff_thumbnail_b64", dtype=str).save()
ln.Feature(name="study", dtype=str).save()
ln.Feature(name="notes", dtype=str).save()
for i in range(20):
    p = f"/tmp/c{i}.qptiff"; open(p, "w").write(f"slide {i}")
    ln.Artifact(p, key=f"img/c{i}.qptiff").save().features.add_values({
        "qptiff_thumbnail_b64": THUMB,
        "study": f"STUDY-{i % 3}",
        # Long free text, to exercise truncation without an image.
        "notes": "lorem ipsum " * 200,
    })

sys.path.insert(0, "/explorer")
import agent

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r} (want {want!r})")
    if not ok: fails.append(label)

def size(payload):
    return len(agent._as_tool_content(payload))

q = agent.blank_query("Artifact")
print(f"     one thumbnail is {len(THUMB)} chars; 20 rows carry {len(THUMB) * 20} of them")

# --- preview sends names and a count, never values --------------------------------------------
seen, _, full = agent._apply(q, "preview", {"feature_columns": ["qptiff_thumbnail_b64"]})
eq("preview sends no rows to the model", "rows" in seen, False)
eq("preview still names the columns", "qptiff_thumbnail_b64" in seen["columns"], True)
eq("preview reports the count", seen["count"], 20)
eq("the full result still reaches the page", len(full["rows"]), 20)
eq("and the page's copy does contain the image",
   full["rows"][0][full["columns"].index("qptiff_thumbnail_b64")].startswith("data:image/"), True)
eq("no thumbnail reaches the model", THUMB[:64] in agent._as_tool_content(seen), False)
eq("preview payload is small", size(seen) < 2000, True)

# --- available_fields sends names, not values --------------------------------------------------
seen, _, _ = agent._apply(q, "available_fields", {})
eq("available_fields sends no values", "feature_values" in seen, False)
eq("available_fields still names the features",
   "qptiff_thumbnail_b64" in seen["features_on_matched_rows"], True)
eq("available_fields payload is small", size(seen) < 3000, True)

# --- field_values returns values, bounded -----------------------------------------------------
seen, _, _ = agent._apply(q, "field_values", {"field": "study"})
eq("field_values returns the distinct values", sorted(seen["values"]),
   ["STUDY-0", "STUDY-1", "STUDY-2"])

seen, _, _ = agent._apply(q, "field_values", {"field": "qptiff_thumbnail_b64"})
text = agent._as_tool_content(seen)
eq("even asked directly, a whole image is not sent", THUMB[:200] in text, False)
eq("the value is truncated with its real length noted", "truncated" in text, True)
eq("field_values on an image column stays bounded", len(text) < agent.MAX_TOOL_CHARS + 200, True)

seen, _, _ = agent._apply(q, "field_values", {"field": "notes"})
eq("a long text value is truncated too",
   len(seen["values"][0]) <= len("lorem ipsum " * 200), True)
eq("notes payload bounded", size(seen) < agent.MAX_TOOL_CHARS + 200, True)

# --- summarise ---------------------------------------------------------------------------------
seen, _, _ = agent._apply(q, "summarise", {"op": "count"})
eq("count is tiny", size(seen) < 400, True)
seen, _, _ = agent._apply(q, "summarise", {"op": "describe", "target": "qptiff_thumbnail_b64"})
eq("describe on an image column stays bounded",
   size(seen) < agent.MAX_TOOL_CHARS + 200, True)

# --- the unconditional ceiling ------------------------------------------------------------------
eq("an absurd payload is capped",
   len(agent._as_tool_content({"x": ["y" * 10000 for _ in range(1000)]}))
   <= agent.MAX_TOOL_CHARS + 200, True)

# --- the whole turn, measured ------------------------------------------------------------------
total = 0
for tool, args in [("available_fields", {}),
                   ("field_values", {"field": "study"}),
                   ("preview", {"feature_columns": ["qptiff_thumbnail_b64"]}),
                   ("summarise", {"op": "count"})]:
    seen, _, _ = agent._apply(q, tool, args)
    total += size(seen)
print(f"     a four-tool turn against image-bearing rows: {total:,} chars (~{total // 4:,} tokens)")
eq("a whole turn stays well inside any context window", total < 20_000, True)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
