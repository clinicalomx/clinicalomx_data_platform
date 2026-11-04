"""Queries return more than lamindb's default of 20 rows."""
import sys
import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/ltstore", name="lt")
import lamindb as ln
ln.Feature(name="batch", dtype=str).save()
N = 60
for i in range(N):
    p = f"/tmp/n{i}.txt"; open(p, "w").write(f"distinct payload {i}")
    a = ln.Artifact(p, key=f"k/n{i}.txt").save()
    a.features.add_values({"batch": f"B{i % 3}"})

sys.path.insert(0, "/explorer")
import lamin_api as api

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got} (want {want})")
    if not ok: fails.append(label)

eq("instance size", ln.Artifact.filter().count(), N)
eq("display limit 5",  api.run_query("Artifact", [], False, 5)["count"], 5)
eq("display limit 25 (over lamindb's 20)", api.run_query("Artifact", [], False, 25)["count"], 25)
eq("display limit 60", api.run_query("Artifact", [], False, 60)["count"], N)
eq("display limit above the data", api.run_query("Artifact", [], False, 500)["count"], N)
eq("merge does not re-cap", len(api.run_query("Artifact", [], False, 50, "all", ["batch"])["rows"]), 50)

csv, rows, truncated, _ = api.export_csv("Artifact", [])
eq("export ignores the display limit", rows, N)
eq("export csv line count", len(csv.strip().split("\n")) - 1, N)
eq("export not truncated", truncated, False)

csv, rows, _, _ = api.export_csv("Artifact", [{"field": "batch", "lookup": "exact", "value": "B0"}])
eq("export honours a feature filter", rows, len([i for i in range(N) if i % 3 == 0]))

csv, rows, _, _ = api.export_csv("Artifact", [], feature_columns=["batch"])
eq("export with merge, all rows", rows, N)
eq("merged column populated on every row",
   sum(1 for line in csv.strip().split("\n")[1:] if ",B" in line), N)

api.EXPORT_MAX_ROWS = 10
csv, rows, truncated, _ = api.export_csv("Artifact", [])
eq("export cap enforced", rows, 10)
eq("export cap reported", truncated, True)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
