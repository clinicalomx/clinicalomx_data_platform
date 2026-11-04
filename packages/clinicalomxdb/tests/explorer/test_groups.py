"""Two-level AND/OR groups, such as f1 AND (f2 OR f3). A flat filter list still works."""
import sys
import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/grpstore", name="grp")
import lamindb as ln

ln.Feature(name="study", dtype=str).save()
# key            study
# img/a.qptiff   STUDY-A
# img/b.qptiff   STUDY-B
# img/c.qptiff   STUDY-C
# rna/a.h5ad     STUDY-A
for key, study in [("img/a.qptiff", "STUDY-A"), ("img/b.qptiff", "STUDY-B"),
                   ("img/c.qptiff", "STUDY-C"), ("rna/a.h5ad", "STUDY-A")]:
    path = "/tmp/" + key.replace("/", "_")
    open(path, "w").write("payload " + key)
    ln.Artifact(path, key=key).save().features.add_values({"study": study})

sys.path.insert(0, "/explorer")
import lamin_api as api

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got} (want {want})")
    if not ok: fails.append(label)

def keys(groups, outer="all"):
    r = api.run_query("Artifact", None, False, 50, outer, None, groups)
    i = r["columns"].index("key")
    return sorted(row[i] for row in r["rows"])

G = lambda match, *fs: {"match": match, "filters": [
    {"field": f[0], "lookup": f[1], "value": f[2]} for f in fs]}

# qptiff AND (STUDY-A OR STUDY-B)
eq("f1 AND (f2 OR f3)",
   keys([G("all", ("key", "icontains", ".qptiff")),
         G("any", ("study", "exact", "STUDY-A"), ("study", "exact", "STUDY-B"))]),
   ["img/a.qptiff", "img/b.qptiff"])

# The same conditions as a flat OR give a broader result.
eq("flat OR of the same three is broader",
   keys([G("any", ("key", "icontains", ".qptiff"),
                  ("study", "exact", "STUDY-A"), ("study", "exact", "STUDY-B"))]),
   ["img/a.qptiff", "img/b.qptiff", "img/c.qptiff", "rna/a.h5ad"])

# (qptiff AND STUDY-C) OR (h5ad AND STUDY-A)
eq("(f1 AND f2) OR (f3 AND f4)",
   keys([G("all", ("key", "icontains", ".qptiff"), ("study", "exact", "STUDY-C")),
         G("all", ("key", "icontains", ".h5ad"), ("study", "exact", "STUDY-A"))], outer="any"),
   ["img/c.qptiff", "rna/a.h5ad"])

eq("two AND groups intersect",
   keys([G("all", ("key", "icontains", ".qptiff")), G("all", ("study", "exact", "STUDY-A"))]),
   ["img/a.qptiff"])

eq("column-only OR group still uses Q",
   keys([G("any", ("key", "icontains", "img/a"), ("key", "icontains", "rna"))]),
   ["img/a.qptiff", "rna/a.h5ad"])

eq("empty groups return everything", len(keys([])), 4)

# The flat shape.
flat = api.run_query("Artifact", [{"field": "key", "lookup": "icontains", "value": ".qptiff"}])
eq("flat filters unchanged", flat["count"], 3)
flat_or = api.run_query("Artifact", [{"field": "study", "lookup": "exact", "value": "STUDY-A"},
                                     {"field": "study", "lookup": "exact", "value": "STUDY-B"}],
                        False, 50, "any")
eq("flat match=any unchanged", flat_or["count"], 3)

# Every other output follows the groups too.
g = [G("all", ("key", "icontains", ".qptiff")),
     G("any", ("study", "exact", "STUDY-A"), ("study", "exact", "STUDY-B"))]
eq("aggregate follows groups",
   api.aggregate("Artifact", None, None, "count", "all", g)["rows"][0][0], "2")
eq("unique follows groups",
   sorted(r[0] for r in api.aggregate("Artifact", None, "study", "unique", "all", g)["rows"]),
   ["STUDY-A", "STUDY-B"])
_, rows, _, _ = api.export_csv("Artifact", None, False, "all", None, None, g)
eq("export follows groups", rows, 2)
eq("scope follows groups",
   [f["name"] for f in api.features_in_scope("Artifact", None, "all", g)], ["study"])

for label, bad in [("bad outer", ("nope", g)), ("bad inner", ("all", [{"match": "nope",
                    "filters": [{"field": "key", "lookup": "exact", "value": "x"}]}]))]:
    try:
        api.run_query("Artifact", None, False, 10, bad[0], None, bad[1])
        fails.append(label); print(f"FAIL {label}: not rejected")
    except ValueError as e:
        print(f"OK   {label} rejected: {str(e)[:70]}")


# --- the "in" lookup takes a list, not one comma-separated string ----------------------------
eq("in with a list operand",
   keys([G("all", ("study", "in", ["STUDY-A", "STUDY-B"]))]),
   ["img/a.qptiff", "img/b.qptiff", "rna/a.h5ad"])

eq("in still accepts a comma string, for hand-written URLs",
   keys([G("all", ("study", "in", "STUDY-A,STUDY-B"))]),
   ["img/a.qptiff", "img/b.qptiff", "rna/a.h5ad"])

eq("in composes with another group",
   keys([G("all", ("key", "icontains", ".qptiff")),
         G("all", ("study", "in", ["STUDY-A", "STUDY-C"]))]),
   ["img/a.qptiff", "img/c.qptiff"])

eq("in on a column",
   keys([G("all", ("key", "in", ["img/b.qptiff", "rna/a.h5ad"]))]),
   ["img/b.qptiff", "rna/a.h5ad"])

eq("a value containing a comma survives the list form",
   keys([G("all", ("study", "in", ["STUDY-A"]))]),
   ["img/a.qptiff", "rna/a.h5ad"])

# An empty "in" is refused, not run as __in=[].
for label, operand in [("empty list", []), ("blank string", ""), ("only separators", "  , ")]:
    try:
        keys([G("all", ("study", "in", operand))])
        fails.append(f"in {label}"); print(f"FAIL in {label}: not rejected")
    except ValueError as e:
        print(f"OK   in {label} rejected: {str(e)[:60]}")

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
