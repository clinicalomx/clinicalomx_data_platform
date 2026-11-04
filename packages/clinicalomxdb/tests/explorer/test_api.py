"""Exercise every lamin_api entry point against a throwaway instance."""
import json, sys, traceback, warnings

import lamindb_setup as ln_setup
try:
    ln_setup.init(storage="/tmp/apistore", name="apitest")   # inside the container, never the mount
except Exception as e:
    print("init:", e)

import lamindb as ln

# A small instance: a categorical feature, an entity type with instances, and an artifact
# with a feature value.
t_patient = ln.Record(name="Patient", is_type=True).save()
t_sample = ln.Record(name="Sample", is_type=True).save()
for i in range(3):
    ln.Record(name=f"P00{i}", type=t_patient).save()
ln.Record(name="S001", type=t_sample).save()

t_celltype = ln.ULabel(name="CellType", is_type=True).save()
for name in ("T cell", "B cell", "Macrophage"):
    ln.ULabel(name=name, type=t_celltype).save()

ln.Feature(name="cell_type", dtype=t_celltype).save()   # type record -> cat[ULabel[<uid>]]
ln.Feature(name="n_cells", dtype="int").save()

sys.path.insert(0, "/explorer")
import lamin_api as api

failures = []

def check(label, fn, show=True):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)   # fail on deprecated API use
            out = fn()
        text = json.dumps(out, default=str)
        print(f"OK   {label}: {text[:260]}")
        return out
    except Exception as e:
        failures.append(label)
        print(f"FAIL {label}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None

print("=" * 70)
regs = check("list_registries", api.list_registries)
if regs:
    names = [r["name"] for r in regs]
    print(f"     registries found: {names}")
    assert "Artifact" in names and "Record" in names, "core registries missing"
    assert "JsonValue" not in names, "hidden registry leaked"

check("registry_fields(Artifact)", lambda: [f["path"] for f in api.registry_fields("Artifact")])
check("registry_fields(Record)", lambda: [f["path"] for f in api.registry_fields("Record")])
check("list_features", api.list_features)
check("list_entity_types", api.list_entity_types)

check("run_query plain", lambda: api.run_query("Record", [], False, 10))
check("run_query filtered",
      lambda: api.run_query("Record", [{"field": "name", "lookup": "exact", "value": "P001"}], False, 10))
check("run_query relation",
      lambda: api.run_query("Record", [{"field": "type__name", "lookup": "exact", "value": "Patient"}], False, 10))
check("run_query icontains",
      lambda: api.run_query("Record", [{"field": "name", "lookup": "icontains", "value": "P0"}], False, 10))
check("run_query features=True", lambda: api.run_query("Artifact", [], True, 10))
check("build_expression", lambda: api.build_expression(
      "Record", api.filter_terms("Record", [{"field": "type__name", "lookup": "exact", "value": "Patient"}]),
      True, 100))

# Negative paths must raise.
def must_raise(label, fn, exc):
    try:
        fn()
    except exc as e:
        print(f"OK   {label} rejected: {type(e).__name__}: {str(e)[:90]}")
        return
    except Exception as e:
        failures.append(label)
        print(f"FAIL {label} raised {type(e).__name__}, expected {exc.__name__}")
        return
    failures.append(label)
    print(f"FAIL {label} did NOT raise")

must_raise("unknown registry", lambda: api.run_query("Nope", [], False, 10), LookupError)
must_raise("unknown field",
           lambda: api.run_query("Record", [{"field": "evil", "lookup": "exact", "value": "x"}], False, 10),
           ValueError)
must_raise("injection via field",
           lambda: api.run_query("Record", [{"field": "name; DROP TABLE", "lookup": "exact", "value": "x"}], False, 10),
           ValueError)

# limit clamping
out = check("limit clamped above MAX_ROWS", lambda: api.run_query("Record", [], False, 999999))
if out:
    assert "[:1000]" in out["expression"], f"limit not clamped: {out['expression']}"
    print("     limit clamped to MAX_ROWS in the shown expression")

recs = ln.Record.objects.filter(name="P001").first()
check("record_detail", lambda: api.record_detail("Record", recs.uid))

# connection_fields on a URL that carries a credential.
# The password is never read from the parse, so it cannot reach the page.
# Both halves of the userinfo are percent-encoded, so a naive split on @ or :
# fails both assertions.
def _asserted(label, condition, detail):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}: {detail}")


CREDENTIAL = "p@ss/w0rd-n0t-in-the-page-9d3f"
fields = api.connection_fields(
    "postgresql://fm%40reader:p%40ss%2Fw0rd-n0t-in-the-page-9d3f@vm.example.org:5432/lamindb")
_asserted("connection_fields reads the four fields off a URL carrying a credential",
          fields == {"host": "vm.example.org", "port": "5432", "database": "lamindb",
                     "role": "fm@reader"}, str(fields))
_asserted("and no field carries the credential, encoded or not",
          not [v for v in fields.values() if CREDENTIAL in v or "p%40ss" in v], str(fields))

# A sqlite instance's endpoint is its file path.
live = check("instance_connection", api.instance_connection)
_asserted("instance_connection reports an endpoint for a connected instance",
          bool(live and (live["database"] or live["host"])), str(live))

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
