"""Feature filtering through the explorer's own API, against a real instance."""
import sys, warnings
import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/ftstore", name="fttest")
import lamindb as ln

ln.Feature(name="qptiff_camera_name", dtype=str).save()
scanner = ln.ULabel(name="Scanner", is_type=True).save()
for n in ("Vectra", "PhenoImager"):
    ln.ULabel(name=n, type=scanner).save()
ln.Feature(name="scanner", dtype=scanner).save()
ln.Feature(name="n_channels", dtype=int).save()
# Collides with an Artifact column, so it must be refused.
ln.Feature(name="description", dtype=str).save()

for i, (cam, scan, n) in enumerate([("CamA-01", "Vectra", 4), ("CamB-02", "PhenoImager", 7)]):
    p = f"/tmp/i{i}.txt"; open(p, "w").write(f"x{i}")
    a = ln.Artifact(p, key=f"roi/i{i}.txt").save()
    a.features.add_values({"qptiff_camera_name": cam, "scanner": scan, "n_channels": n})
open("/tmp/plain.txt", "w").write("p")
ln.Artifact("/tmp/plain.txt", key="roi/plain.txt").save()

sys.path.insert(0, "/explorer")
import lamin_api as api

# A vocabulary past the cap, so list_features must report its value list as incomplete.
crowded = ln.ULabel(name="Crowded", is_type=True).save()
for i in range(api.MAX_VALUES + 1):
    ln.ULabel(name=f"C{i:04d}", type=crowded).save()
ln.Feature(name="crowded_label", dtype=crowded).save()

fails = []
def q(label, filters, expect):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            r = api.run_query("Artifact", filters, False, 50)
        ok = r["count"] == expect
        print(f"{'OK  ' if ok else 'FAIL'} {label}: {r['count']} (want {expect})  {r['expression']}")
        if not ok: fails.append(label)
        return r
    except Exception as e:
        fails.append(label); print(f"FAIL {label}: {type(e).__name__}: {str(e)[:160]}")

def rejected(label, filters):
    try:
        api.run_query("Artifact", filters, False, 50)
    except ValueError as e:
        print(f"OK   {label} rejected: {str(e)[:120]}"); return
    except Exception as e:
        fails.append(label); print(f"FAIL {label}: wrong error {type(e).__name__}: {e}"); return
    fails.append(label); print(f"FAIL {label}: was NOT rejected")

specs = {f["path"]: f for f in api.registry_fields("Artifact")}
feats = [p for p, s in specs.items() if s["type"] == "feature"]
print("features offered on Artifact:", feats)
assert "qptiff_camera_name" in feats, "the feature is not offered"
print("shadowed entry:", {k: v for k, v in specs["description"].items() if k in ("type", "shadowed", "lookups")})
print("scanner values:", specs["scanner"]["values"])
print()

listed = {f["name"]: f for f in api.list_features()}
if listed["scanner"]["values_incomplete"]:
    fails.append("whole vocabulary reported as partial")
    print("FAIL a vocabulary read whole is reported as partial:", listed["scanner"])
if not listed["crowded_label"]["values_incomplete"]:
    fails.append("partial vocabulary reported as whole")
    print("FAIL a vocabulary read past the cap is reported as whole:",
          len(listed["crowded_label"]["values"]), "values")
print("crowded_label:", len(listed["crowded_label"]["values"]), "of",
      api.MAX_VALUES + 1, "values, partial:", listed["crowded_label"]["values_incomplete"])

q("feature exact",      [{"field": "qptiff_camera_name", "lookup": "exact", "value": "CamA-01"}], 1)
q("feature icontains",  [{"field": "qptiff_camera_name", "lookup": "icontains", "value": "cama"}], 1)
q("feature startswith",[{"field": "qptiff_camera_name", "lookup": "startswith", "value": "Cam"}], 2)
q("HAS the feature",    [{"field": "qptiff_camera_name", "lookup": "isset", "value": ""}], 2)
q("LACKS the feature",  [{"field": "qptiff_camera_name", "lookup": "notset", "value": ""}], 1)
q("categorical exact",  [{"field": "scanner", "lookup": "exact", "value": "Vectra"}], 1)
q("numeric gt",         [{"field": "n_channels", "lookup": "gt", "value": "5"}], 1)
q("feature + column",   [{"field": "qptiff_camera_name", "lookup": "isset", "value": ""},
                         {"field": "key", "lookup": "icontains", "value": "i0"}], 1)
q("two features",       [{"field": "scanner", "lookup": "exact", "value": "Vectra"},
                         {"field": "n_channels", "lookup": "exact", "value": "4"}], 1)

rejected("shadowed feature", [{"field": "description", "lookup": "icontains", "value": "x"}])
rejected("bad lookup on feature",
         [{"field": "scanner", "lookup": "icontains", "value": "V"}])
rejected("unknown feature", [{"field": "not_a_feature", "lookup": "exact", "value": "x"}])

r = q("features column included",
      [{"field": "qptiff_camera_name", "lookup": "isset", "value": ""}], 2)
r2 = api.run_query("Artifact", [{"field": "qptiff_camera_name", "lookup": "isset", "value": ""}], True, 50)
print("with include=features, columns:", r2["columns"])
if "qptiff_camera_name" not in r2["columns"]:
    fails.append("feature column missing"); print("FAIL feature column missing")

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
