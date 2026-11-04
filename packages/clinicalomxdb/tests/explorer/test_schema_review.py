"""The schema review page, exercised offline against fixtures.

Never imports lamindb or lamin_api: importing lamin_api connects whatever instance the host
is configured for.
"""

import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "schema_review"

# make explorer-test mounts the review directory at /review; CI uses PYTHONPATH.
sys.path.insert(0, "/review")

import compare              # noqa: E402
import declared as declared_mod  # noqa: E402
import render               # noqa: E402
import schema_review        # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def verdict_of(rows, name, key="name"):
    for row in rows:
        if row[key] == name:
            return row["verdict"]
    return None


print("=" * 70)

# --- the declaration reads back as declared -----------------------------------------------
declaration = declared_mod.load(FIXTURES / "declared", "0.2.6")
names = [f["name"] for f in declaration["features"]]
check("declaration reads every feature row", len(names) == 13, str(names))
check("provenance comments are not data", "# dtype" not in "".join(names))
check("a blank status column means active",
      next(f for f in declaration["features"] if f["name"] == "age")["status"] == "active")
check("a deprecated row keeps its status",
      next(f for f in declaration["features"] if f["name"] == "age_at_itx")["status"]
      == "deprecated")
check("types carry their tripwire uid",
      next(t for t in declaration["types"] if t["name"] == "Cohort")["uid"]
      == "CEL53rJWA2uHCGoQ")
check("a subtype keeps its parent",
      next(t for t in declaration["types"] if t["name"] == "Core")["parent"] == "ROI")
check("storages read by root",
      {s["root"] for s in declaration["storages"]}
      == {"/rdm/Q0000", "/scratch/project_mnt/S0000/dbstorage"})

# --- both declaration reads are utf-8, whatever the host locale says -----------------------
# Runs in a child under an ascii locale. PYTHONIOENCODING changes only the child's stdout.
UNIT_TEXT = "5 µm sections, Brückner stain"
READ_DECLARATION = """
import pathlib
import sys

import declared

declaration = declared.load(pathlib.Path(sys.argv[1]), "0.2.6")
print(next(f for f in declaration["features"] if f["name"] == "cell_type")["description"])
print(next(t for t in declaration["types"] if t["name"] == "Cohort")["description"])
"""
with tempfile.TemporaryDirectory() as tmp:
    scratch = pathlib.Path(tmp)
    (scratch / declared_mod.FEATURES).write_text(
        f'name,dtype,description,coerce,status\ncell_type,cat[ULabel],"{UNIT_TEXT}",,active\n',
        encoding="utf-8")
    (scratch / declared_mod.TYPES).write_text(
        f"types:\n- name: Cohort\n  uid: CEL53rJWA2uHCGoQ\n  description: {UNIT_TEXT}\n",
        encoding="utf-8")
    (scratch / declared_mod.STORAGES).write_text("storages: {}\n", encoding="utf-8")

    ascii_locale = dict(os.environ, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0",
                        PYTHONUTF8="0", PYTHONIOENCODING="utf-8",
                        PYTHONPATH=os.pathsep.join(
                            [str(pathlib.Path(declared_mod.__file__).resolve().parent)]
                            + [p for p in sys.path if p]))
    read = subprocess.run([sys.executable, "-c", READ_DECLARATION, str(scratch)],
                          capture_output=True, text=True, encoding="utf-8", env=ascii_locale)
    check("a utf-8 declaration reads under an ascii locale",
          read.returncode == 0, read.stderr.strip()[-200:])
    check("features.csv keeps every character it declared",
          read.stdout.splitlines()[:1] == [UNIT_TEXT], read.stdout)
    check("types.yaml keeps every character it declared",
          read.stdout.splitlines()[1:2] == [UNIT_TEXT], read.stdout)

# --- a declaration that reads as empty is refused, not compared ----------------------------
with tempfile.TemporaryDirectory() as tmp:
    scratch = pathlib.Path(tmp)

    def refusal(**files):
        for filename, text in files.items():
            (scratch / filename).write_text(text)
        try:
            declared_mod.load(scratch)
        except declared_mod.NotFound as exc:
            return str(exc)
        return ""

    unkeyed = refusal(**{declared_mod.FEATURES: "feature,dtype\nage,int\n"})
    check("a features.csv with no name column refuses to load, naming its columns",
          "feature, dtype" in unkeyed, unkeyed)
    check("a features.csv carrying no declaration refuses to load",
          refusal(**{declared_mod.FEATURES: "# provenance\n"}))
    # A dtype column the reader cannot key on fails loudly.
    no_dtype = refusal(**{declared_mod.FEATURES: "name,type,status\nage,int,active\n"})
    check("a features.csv with no dtype column refuses to load, naming its columns",
          "dtype" in no_dtype and "name, type, status" in no_dtype, no_dtype)
    mis_keyed = refusal(**{declared_mod.FEATURES: "name,dtype\nage,int\n",
                           declared_mod.TYPES: "entities:\n- name: Cohort\n"})
    check("a types.yaml under an unexpected top-level key refuses to load",
          "entities" in mis_keyed, mis_keyed)
    check("a declaration this reader can key on loads",
          not refusal(**{declared_mod.TYPES: "types:\n- name: Cohort\n"}))

# --- an installed package layout has to carry all three files ------------------------------
# A directory named with --declared skips this check.
with tempfile.TemporaryDirectory() as tmp:
    site = pathlib.Path(tmp)
    data = site / "fakeomx" / "data"
    data.mkdir(parents=True)
    (site / "fakeomx" / "__init__.py").write_text("")
    (data / declared_mod.FEATURES).write_text("name,dtype\nage,int\n")
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()

    def located():
        try:
            return str(declared_mod.find_data_dir("fakeomx"))
        except declared_mod.NotFound as exc:
            return f"NotFound {exc}"

    no_types = located()
    check("an installed layout missing types.yaml is refused, naming the file",
          no_types.startswith("NotFound") and declared_mod.TYPES in no_types, no_types)
    check("the refusal names every candidate path it tried",
          all(str(site / "fakeomx" / candidate) in no_types for candidate in ("vocab/data",
                                                                             "data")),
          no_types)
    (data / declared_mod.TYPES).write_text("types: []\n")
    no_storages = located()
    check("an installed layout missing storages.yaml is refused, naming the file",
          no_storages.startswith("NotFound") and declared_mod.STORAGES in no_storages,
          no_storages)
    (data / declared_mod.STORAGES).write_text("storages: []\n")
    check("an installed layout carrying all three is located", located() == str(data))

    # A stale partial directory is skipped, not the end of the search.
    stale = site / "fakeomx" / "vocab" / "data"
    stale.mkdir(parents=True)
    (stale / declared_mod.FEATURES).write_text("name,dtype\nage,int\n")
    check("a partial candidate is skipped for a complete one further on", located() == str(data),
          located())

    named = site / "partial"
    named.mkdir()
    (named / declared_mod.FEATURES).write_text("name,dtype\nage,int\n")
    partial = declared_mod.load(named)
    check("a directory named explicitly still loads without types.yaml",
          [f["name"] for f in partial["features"]] == ["age"] and partial["types"] == [])

    sys.path.remove(str(site))
    sys.modules.pop("fakeomx", None)

landed = json.loads((FIXTURES / "landed.json").read_text())
samples = json.loads((FIXTURES / "samples.json").read_text())

# --- declared, did not land ---------------------------------------------------------------
type_uids = {t["name"]: t["uid"] for t in landed["types"]}
registries = compare.registry_spellings(landed["registries"])
features = compare.diff_features(declaration["features"], landed["features"], type_uids,
                                 registries)

check("a bionty registry is confirmable under the spelling lamindb stores",
      "bionty.Protein" in registries and "Protein" not in registries, str(sorted(registries)))

check("a declared feature that did not land is MISSING",
      verdict_of(features, "source_core_uid") == compare.MISSING,
      str(verdict_of(features, "source_core_uid")))
check("MISSING counts as a failure",
      "source_core_uid" in [r["name"] for r in compare.failures(features)])
check("a dtype that disagrees is DTYPE_DRIFT",
      verdict_of(features, "bgnorm_attempts") == compare.DTYPE_DRIFT)
check("a feature that landed as declared is a match",
      verdict_of(features, "roi_uid") == compare.MATCH)
check("type:<Name> is compared against the landed uid, not against its own spelling",
      verdict_of(features, "slide_feature") == compare.MATCH,
      str(next(r for r in features if r["name"] == "slide_feature")))
check("a declared type this reviewer cannot resolve is not reported as drift",
      verdict_of(features, "source_feature") == compare.UNRESOLVED)
check("a bare module-qualified registry is the same declaration as its cat[...] spelling",
      verdict_of(features, "marker_target") == compare.MATCH,
      str(next(r for r in features if r["name"] == "marker_target")))
# A capitalised token the landed side cannot vouch for may be a bare type name.
check("a bare registry the landed side cannot vouch for is unresolved, not drift",
      verdict_of(features, "parent_sample") == compare.UNRESOLVED,
      str(next(r for r in features if r["name"] == "parent_sample")))
check("an unresolved bare registry is not asserted against the instance",
      next(r for r in features if r["name"] == "parent_sample")["expected_dtype"] == "Sample")
check("both unconfirmable dtypes are counted as unresolved",
      sorted(r["name"] for r in compare.unresolved(features))
      == ["parent_sample", "source_feature"],
      str(sorted(r["name"] for r in compare.unresolved(features))))
check("an unresolved row is never a failure",
      not [r for r in compare.failures(features) if r["verdict"] == compare.UNRESOLVED])
# A blank dtype cell is no claim, not a match.
blank_declared = [{"name": "roi_uid", "dtype": "", "description": "", "status": "active"}]
blank_rows = compare.diff_features(blank_declared, landed["features"], type_uids, registries)
check("a blank declared dtype is unresolved rather than a match",
      verdict_of(blank_rows, "roi_uid") == compare.UNRESOLVED, str(blank_rows[0]))
check("a blank declared dtype feeds the unconfirmed count and never the failure count",
      [r["name"] for r in compare.unresolved(blank_rows)] == ["roi_uid"]
      and not compare.failures(blank_rows))
check("a blank declared dtype that also did not land is still reported as missing",
      verdict_of(compare.diff_features(
          [{"name": "never_landed", "dtype": "", "description": "", "status": "active"}],
          landed["features"], type_uids, registries), "never_landed") == compare.MISSING)
check("a deprecated feature that is absent is not a failure",
      verdict_of(features, "age_at_itx") == compare.RETIRED)
check("an undeclared feature is reported and never fails",
      verdict_of(features, "bgnormed_image") == compare.EXTRA)
check("only the two real defects fail the feature panel",
      sorted(r["name"] for r in compare.failures(features))
      == ["bgnorm_attempts", "source_core_uid"],
      str(sorted(r["name"] for r in compare.failures(features))))

type_rows = compare.diff_types(declaration["types"], landed["types"])
check("a type deleted and recreated under a new uid is drift",
      verdict_of(type_rows, "Patient") == compare.DTYPE_DRIFT)
check("a declared type that did not land is MISSING",
      verdict_of(type_rows, "Core") == compare.MISSING)
check("an undeclared type is reported and never fails",
      verdict_of(type_rows, "Slide") == compare.EXTRA)

storages = compare.diff_storages(declaration["storages"], landed["storages"])
check("a declared storage root that did not land is MISSING",
      verdict_of(storages, "/scratch/project_mnt/S0000/dbstorage", key="root")
      == compare.MISSING)
check("an unregistered extra root is reported and never fails",
      verdict_of(storages, "/mnt/data/artifacts", key="root") == compare.EXTRA)
check("a declared storage row is labelled with the declared key",
      next(r for r in storages if r["root"] == "/rdm/Q0000")["label"] == "q0000")
check("a storage root nobody declared is labelled with what lamindb has",
      next(r for r in storages if r["root"] == "/mnt/data/artifacts")["label"]
      == "control-plane local artifacts")

# --- row validation calls the fleet's validators, it does not carry a copy -----------------
# The rules are lamin_api.feature_kind and lamin_api.scalar, passed in by collect.rules.
# Swapping them must move the verdicts.
REAL_VALUES = {"str": "str", "int": "num", "float": "num", "bool": "bool"}


def real_kind(dtype):
    return "cat" if (dtype or "").startswith("cat[") else REAL_VALUES.get(dtype, "str")


def real_scalar(value, spec):
    if real_kind(spec.get("dtype", "")) == "num":
        try:
            return int(value)
        except ValueError:
            return float(value)
    return value


specs = {f["name"]: f for f in landed["features"]}
problems = []
skipped = []
judged = 0
absent = []
for sample in samples:
    result = compare.validate_sample(sample, specs, real_kind, real_scalar)
    problems += result["problems"]
    skipped += result["skipped"]
    judged += result["judged"]
    absent += [(sample["registry"], name) for name in result["absent_columns"]]

flagged = {(p["identity"], p["column"]) for p in problems}
held = {(s["identity"], s["column"]): s["reason"] for s in skipped}
check("a value outside a categorical feature's permitted values is flagged",
      ("a2QrTz01", "cell_type") in flagged, str(sorted(flagged)))
check("a non-numeric value in a numeric feature is flagged",
      ("a3QrTz02", "n_cells") in flagged)
check("a multi-valued cell of permitted values is not flagged",
      ("a5QrTz04", "cell_type") not in flagged)
check("a permitted value that itself contains the separator is not flagged",
      ("a6QrTz05", "cell_type") not in flagged, str(sorted(flagged)))
check("a multi-valued numeric cell is not flagged",
      ("a6QrTz05", "n_cells") not in flagged, str(sorted(flagged)))
check("an empty feature cell is data, not a defect",
      not [p for p in problems if p["identity"] == "a4QrTz03"])
check("exactly the two bad cells are flagged", len(problems) == 2, str(problems))

check("an unjudged cell is counted apart from a clean one and never as a problem",
      len(skipped) == 5 and judged == 14 and not (flagged & set(held)),
      f"{len(skipped)} skipped, {judged} judged")
check("an empty cell is neither judged nor skipped",
      not [s for s in skipped if s["identity"] == "a4QrTz03"])
check("a merged multi-valued cell carries the reason it could not be decomposed",
      held.get(("a5QrTz04", "cell_type")) == compare.MULTI_VALUED
      and held.get(("a6QrTz05", "n_cells")) == compare.MULTI_VALUED, str(held))
check("a categorical with no permitted value carries its own reason",
      held.get(("r1QrTz00", "annotations")) == compare.NO_PERMITTED_VALUES, str(held))
check("a merged feature that reached no column is reported rather than dropped",
      absent == [("Collection", "cell_type")], str(absent))
collection = compare.validate_sample(samples[2], specs, real_kind, real_scalar)
check("a frame lacking uid judges nothing", collection["judged"] == 0
      and not collection["problems"] and not collection["skipped"], str(collection))

thin = compare.validate_sample(samples[0], dict(specs, cell_type=dict(specs["cell_type"],
                                                                      values=[])),
                               real_kind, real_scalar)
check("an empty permitted-value list is a reason, not a verdict",
      {s["reason"] for s in thin["skipped"] if s["column"] == "cell_type"}
      == {compare.NO_PERMITTED_VALUES},
      str(thin["skipped"]))

# Every dtype classified as free text: nothing can be flagged.
as_text = []
as_text_skipped = []
for sample in samples:
    result = compare.validate_sample(sample, specs, lambda dtype: "str", real_scalar)
    as_text += result["problems"]
    as_text_skipped += result["skipped"]
check("the injected kind rule decides what is checked", not as_text, str(as_text))
check("the injected kind rule decides what is left unjudged", not as_text_skipped,
      str(as_text_skipped))

# Every dtype numeric and every conversion refused: every non-empty feature cell is flagged.
as_refused = []
for sample in samples:
    as_refused += compare.validate_sample(
        sample, specs, lambda dtype: "num",
        lambda value, spec: (_ for _ in ()).throw(ValueError("refused")))["problems"]
check("the injected scalar rule decides what is refused", len(as_refused) == 16,
      str(len(as_refused)))
check("a multi-valued cell is unjudged even when every conversion is refused",
      not [p for p in as_refused if ", " in p["value"]],
      str([p["value"] for p in as_refused if ", " in p["value"]]))

# A partly read categorical cannot be judged. The list is shorter than the cap after
# deduplication, so its length cannot signal truncation.
truncated = dict(specs)
truncated["cell_type"] = dict(specs["cell_type"], values=["B cell", "Macrophage", "T cell"],
                              values_incomplete=True)
partial = compare.validate_sample(samples[0], truncated, real_kind, real_scalar)
check("a permitted-value list lamin_api only partly read suspends the categorical check",
      not [p for p in partial["problems"] if p["column"] == "cell_type"],
      str(partial["problems"]))
check("a suspended categorical check says the partial read is why",
      {s["reason"] for s in partial["skipped"] if s["column"] == "cell_type"}
      == {compare.PERMITTED_VALUES_INCOMPLETE},
      str(partial["skipped"]))
whole = dict(specs)
whole["cell_type"] = dict(specs["cell_type"], values=["B cell", "Macrophage", "T cell"],
                          values_incomplete=False)
complete = compare.validate_sample(samples[0], whole, real_kind, real_scalar)
check("a complete permitted-value list still judges the cell",
      [p["identity"] for p in complete["problems"] if p["column"] == "cell_type"]
      == ["a2QrTz01"], str(complete["problems"]))
check("a value the partial list carries is still judged",
      not [s for s in partial["skipped"] if (s["identity"], s["column"]) == ("a1QrTz00",
                                                                            "cell_type")],
      str(partial["skipped"]))

# --- the rules really are lamin_api's ------------------------------------------------------
stub = types.ModuleType("lamin_api")
stub.feature_kind = real_kind
stub.scalar = real_scalar
sys.modules["lamin_api"] = stub
import collect  # noqa: E402

check("collect hands over lamin_api's own functions",
      collect.rules() == (stub.feature_kind, stub.scalar))

# --- a disconnected instance is refused, not compared against ------------------------------
stub.list_features = lambda: []
stub.list_entity_types = lambda: []
stub.list_registries = lambda: []
stub.run_query = lambda registry, filters, features, limit: {
    "columns": ["uid", "root", "description"], "rows": [], "truncated": False}
stub.instance_slug = lambda: None
stub.instance_connection = lambda: {"host": "postgres", "port": "5432",
                                    "database": "lamindb", "role": "root"}

try:
    collect.landed()
    disconnected = ""
except RuntimeError as exc:
    disconnected = str(exc)
# The message names lamin-connect for the VM and lamin-local for a developer machine.
check("a disconnected instance refuses rather than reporting every name as missing",
      "lamin-connect" in disconnected and "lamin-local" in disconnected
      and "LAMIN_HOME" in disconnected, disconnected)

stub.instance_slug = lambda: "example-org/clinicalomx"
check("a connected instance reads back under its own slug",
      collect.landed()["instance"] == "example-org/clinicalomx")

# --- the endpoint travels with the slug ----------------------------------------------------
check("the connection endpoint reaches the page alongside the slug",
      collect.landed()["connection"] == {"host": "postgres", "port": "5432",
                                         "database": "lamindb", "role": "root"})


# --- a denied read refuses in the same shape as a missing connection ------------------------
class FakeDenied(Exception):
    pgcode = "42501"


def _denied():
    raise FakeDenied("permission denied for table lamindb_feature")


stub.list_features = _denied
try:
    collect.landed()
    denied = ""
except RuntimeError as exc:
    denied = str(exc)
check("a denied read refuses rather than raising a database traceback",
      "permission denied for table lamindb_feature" in denied and "root" in denied
      and "SELECT" in denied, denied)
check("and the refusal says how a later migration's tables stay readable",
      "ALTER DEFAULT PRIVILEGES" in denied, denied)
check("and it says what this read cost, which is the whole comparison",
      "reported as one that did not land" in denied
      and "cannot be rendered" not in denied, denied)


# Only a denied read is relabelled. Any other database error reaches the caller as itself.
class FakeBroken(Exception):
    pgcode = "42703"


def _broken():
    raise FakeBroken("column lamindb_feature.nope does not exist")


stub.list_features = _broken
try:
    collect.landed()
    other = "nothing raised"
except FakeBroken:
    other = "raised"
except RuntimeError as exc:
    other = f"relabelled: {exc}"
check("a database error that is not a denied read is left alone", other == "raised", other)

stub.list_features = lambda: []


# Every read refuses a missing grant, not only the first.
def _denied_sample(registry, filters, features, limit):
    if registry == "Artifact":
        raise FakeDenied("permission denied for table lamindb_artifact")
    return {"columns": ["uid", "root", "description"], "rows": [], "truncated": False}


stub.run_query = _denied_sample
check("a grant that stops short of one table still reads the rest", collect.landed()["instance"])
try:
    collect.sample("Artifact", 20)
    sampling = ""
except RuntimeError as exc:
    sampling = str(exc)
check("a denied read while sampling refuses rather than raising a database traceback",
      "permission denied for table lamindb_artifact" in sampling
      and "ALTER DEFAULT PRIVILEGES" in sampling, sampling)
check("and names the same role the landed refusal names", "root" in sampling, sampling)
check("and says what this read cost, which is one registry's sampled rows",
      "Artifact" in sampling and "cannot be rendered" in sampling
      and "reported as one that did not land" not in sampling, sampling)

with tempfile.TemporaryDirectory() as tmp:
    partial = pathlib.Path(tmp) / "schema-review.html"
    code = schema_review.main(["--declared", str(FIXTURES / "declared"), "--out", str(partial),
                               "--registry", "Artifact"])
    check("a table the role cannot read exits 2 and writes no page",
          code == 2 and not partial.exists(), f"exit {code}, page {partial.exists()}")


def _broken_sample(registry, filters, features, limit):
    raise FakeBroken("column lamindb_artifact.nope does not exist")


stub.run_query = _broken_sample
try:
    collect.sample("Artifact", 20)
    other_sample = "nothing raised"
except FakeBroken:
    other_sample = "raised"
except RuntimeError as exc:
    other_sample = f"relabelled: {exc}"
check("a database error that is not a denied read is left alone while sampling too",
      other_sample == "raised", other_sample)


# A typo in SCHEMA_REVIEW_REGISTRIES reads as a typo.
def _lookup(registry, filters, features, limit):
    if registry == "Recrd":
        raise LookupError(f"unknown registry {registry!r}")
    return {"columns": ["uid", "root", "description"], "rows": [], "truncated": False}


stub.run_query = _lookup
try:
    collect.sample("Recrd", 20)
    typo = ""
except RuntimeError as exc:
    typo = str(exc)
check("a registry the instance does not have names itself rather than raising a lookup error",
      "Recrd" in typo and "SCHEMA_REVIEW_REGISTRIES" in typo, typo)

with tempfile.TemporaryDirectory() as tmp:
    mistyped = pathlib.Path(tmp) / "schema-review.html"
    code = schema_review.main(["--declared", str(FIXTURES / "declared"), "--out", str(mistyped),
                               "--registry", "Recrd"])
    check("a mistyped registry exits 2 and writes no page",
          code == 2 and not mistyped.exists(), f"exit {code}, page {mistyped.exists()}")

stub.instance_slug = lambda: None
with tempfile.TemporaryDirectory() as tmp:
    page_path = pathlib.Path(tmp) / "schema-review.html"
    code = schema_review.main(["--declared", str(FIXTURES / "declared"), "--out", str(page_path)])
    check("the command exits 2 and writes no page when nothing is connected",
          code == 2 and not page_path.exists(), f"exit {code}, page {page_path.exists()}")

del sys.modules["lamin_api"]


class _RootlessStorage:
    @staticmethod
    def run_query(registry, features, merged, limit):
        return {"columns": ["uid", "description"], "rows": [["st1QrTz", "artifacts"]]}


try:
    collect._storages(_RootlessStorage)
    shape_error = ""
except RuntimeError as exc:
    shape_error = str(exc)
check("a Storage frame carrying no root column is an error", "root" in shape_error, shape_error)


class _TruncatedStorage:
    @staticmethod
    def run_query(registry, features, merged, limit):
        return {"columns": ["uid", "root", "description"],
                "rows": [["st1QrTz", "/rdm/Q0000", "raw"]], "truncated": True}


try:
    collect._storages(_TruncatedStorage)
    truncation_error = ""
except RuntimeError as exc:
    truncation_error = str(exc)
check("a truncated Storage frame is an error", "cap" in truncation_error, truncation_error)

check("an empty registry is not sampled by default",
      collect.choose_registries(landed["registries"] + [{"name": "Record", "count": 0}], None)
      == (["Artifact"], []),
      str(collect.choose_registries(landed["registries"] + [{"name": "Record", "count": 0}],
                                    None)))
check("a populated default registry is sampled",
      collect.choose_registries(landed["registries"], None) == (["Artifact", "Record"], []))
check("an explicitly named registry is sampled whether or not it holds rows",
      collect.choose_registries(landed["registries"], ["Schema"]) == (["Schema"], []))
# Names past the cap are returned, not refused or dropped.
over_long = [f"R{i}" for i in range(50)]
capped_choice, dropped_choice = collect.choose_registries(landed["registries"], over_long)
check("the registry count is capped", len(capped_choice) == collect.MAX_REGISTRIES)
check("every named registry past the cap is handed back rather than dropped",
      capped_choice + dropped_choice == over_long,
      f"{len(capped_choice)} sampled, {len(dropped_choice)} dropped")
del sys.modules["collect"]

# --- the page says so ----------------------------------------------------------------------
context = schema_review.build(declaration, landed, samples, real_kind, real_scalar)
page = render.render(context)

check("the page is one self-contained document",
      page.startswith("<!doctype html>") and page.rstrip().endswith("</html>"))
check("the page pulls in nothing from outside itself",
      "<script" not in page and "src=" not in page and "<link" not in page)

# --- the page names the database it read ----------------------------------------------------
check("the meta box names the host, port, database and role it read",
      "root@postgres:5432/lamindb" in page, page.split("</div>")[1])
check("and carries no credential from the settings file it came from",
      "password" not in page.lower() and "postgresql://" not in page)
no_connection = dict(landed)
del no_connection["connection"]
check("a landed side with no endpoint recorded says so rather than rendering an empty one",
      "not recorded" in render.render(
          schema_review.build(declaration, no_connection, samples, real_kind, real_scalar)))
sqlite_landed = dict(landed, connection={"host": "", "port": "", "role": "",
                                         "database": "/tmp/throwaway/lamindb.db"})
check("a sqlite instance prints its file path, which is the whole endpoint it has",
      "/tmp/throwaway/lamindb.db" in render.render(
          schema_review.build(declaration, sqlite_landed, samples, real_kind, real_scalar)))
callout = page.split('class="banner fail"')[1].split("<h3>")[0]
check("every declared name that did not land is named where it cannot be scrolled past",
      all(fragment in callout
          for fragment in ("feature source_core_uid", "entity type Core",
                           "storage root /scratch/project_mnt/S0000/dbstorage")), callout)
check("the callout accounts for every failure the headline counts",
      "DID NOT LAND (3)" in callout and "DTYPE DISAGREES (2)" in callout, callout)
check("the callout names every declared dtype nobody could confirm",
      "unresolved (2)" in callout and "feature parent_sample" in callout
      and "feature source_feature" in callout, callout)
check("the callout names the declared names that did not land because they are deprecated",
      "deprecated, absent (1)" in callout and "feature age_at_itx" in callout, callout)
check("the schema panel reports failure", 'class="banner fail"' in page)
check("a sampled feature column is marked as one in the header",
      "<th>cell_type (feature)</th>" in page and "<th>uid</th>" in page)
check("the storage table header names both sides of its label column",
      "<th>declared key or landed description</th>" in page)
check("every diff line is an annotation target",
      'id="feature-source_core_uid"' in page and 'id="type-Core"' in page)
check("every sampled row is an annotation target", 'id="row-Artifact-1"' in page)
check("a flagged cell carries its own target",
      'id="flag-Artifact-1-cell_type"' in page)
check("an unjudged cell carries its own target, distinct from a flagged one",
      'id="skip-Artifact-4-cell_type"' in page and 'id="skip-Record-0-annotations"' in page)
check("a flagged cell and an unjudged one are marked apart in the table",
      'class="mono flagged"' in page and 'class="mono unjudged"' in page)
check("the verdict panel counts what the panels found",
      "<strong>5</strong> schema failures" in page
      and "<strong>2</strong> flagged values" in page,
      schema_review.summarise(context))
check("the verdict panel counts what nobody judged",
      "<strong>2</strong> declared dtypes and" in page
      and "<strong>5</strong> sampled cells" in page,
      schema_review.summarise(context))
check("the verdict panel counts the feature columns that never arrived",
      "<strong>1</strong> merged feature columns never reached a frame" in page)
check("the verdict panel names every registry where nothing was judged",
      "No sampled cell was judged in" in page and "Collection</span>" in page)
check("the page names every reason a cell went unjudged",
      f"unjudged (3): {compare.MULTI_VALUED}" in page
      and f"unjudged (2): {compare.NO_PERMITTED_VALUES}" in page)
check("the page says a merged feature reached no column",
      "reached no column in the frame" in page and "cell_type</span>" in page)
check("the panel header leads with how many cells were judged",
      "2 rows, 2 cells judged, 0 flagged, 2 unjudged" in page)
check("the stderr line and the page agree",
      schema_review.summarise(context)
      == ("5 schema failures, 2 declared dtypes unresolved, 1 deprecated names absent, "
          "2 flagged values, 14 cells judged, 5 left unjudged, "
          "1 merged features with no column, "
          "1 registries with no cell judged, 3 registries sampled, "
          "0 named registries not sampled"),
      schema_review.summarise(context))

gaps = schema_review.report(context)
check("the one accounting agrees with the panels it feeds",
      gaps["judged"] == 14 and len(gaps["skipped"]) == 5 and len(gaps["problems"]) == 2
      and len(gaps["unresolved"]) == 2 and len(gaps["absent_columns"]) == 1
      and gaps["unjudged_registries"] == ["Collection"], str(gaps["unjudged_registries"]))
check("no gap is counted as a failure or as a flagged value",
      not [r for r in compare.failures(schema_review.schema_rows(context))
           if r["verdict"] == compare.UNRESOLVED]
      and not [p for p in gaps["problems"] if "reason" in p])
check("the accounting keeps one entry per sample, in order",
      [r["registry"] for r in gaps["registries"]] == ["Artifact", "Record", "Collection"])
check("the page states that it never writes",
      "Never a hand-edit of the instance." in page)
check("the meta box states how large the declared side was",
      "declared: 13 features, 4 entity types, 2 storage roots" in page)

# An unresolved row neither fails a panel nor counts as confirmed.
amber = schema_review.build(
    {"source": "amber", "version": "", "types": [], "storages": [],
     "features": [f for f in declaration["features"] if f["name"] == "source_feature"]},
    landed, [], real_kind, real_scalar)
amber_page = render.render(amber)
check("an unresolved declared dtype alone does not fail the panel",
      'class="banner fail"' not in amber_page)
check("an unresolved declared dtype stops the panel claiming a confirmation",
      'class="banner warn"' in amber_page
      and "no declared name failed, and 1 could not be confirmed either way" in amber_page)
check("a panel that judged no sampled cell says nothing was checked",
      "nothing on this panel was checked" in amber_page)
check("no panel reads green while something went unconfirmed",
      'class="banner pass"' not in amber_page)

blank_run = schema_review.build(
    {"source": "blank", "version": "", "types": [], "storages": [],
     "features": blank_declared},
    landed, [], real_kind, real_scalar)
blank_page = render.render(blank_run)
check("a blank declared dtype stops panel 1 claiming a confirmation",
      'class="banner pass"' not in blank_page.split("2. Sampled rows")[0]
      and "1 could not be confirmed either way" in blank_page,
      blank_page.split("1. Declared versus landed")[1][:600])
check("a blank declared dtype never fails the panel", 'class="banner fail"' not in blank_page)

held_only = schema_review.build({"source": "held", "version": "", "features": [], "types": [],
                                 "storages": []},
                                landed, [samples[1]], real_kind, real_scalar)
held_page = render.render(held_only)
check("a panel that judged some cells and skipped others does not read green",
      'class="banner warn"' in held_page
      and "2 sampled values were judged and none was refused, and 2 were left unjudged"
      in held_page,
      held_page.split('class="banner warn">')[1].split("</div>")[0] if 'class="banner warn"'
      in held_page else "no amber banner")

settled = compare.FAILURES
compare.FAILURES = settled + (compare.UNRESOLVED,)
try:
    widened = render.render(amber)
finally:
    compare.FAILURES = settled
check("the page takes its failing verdicts from compare.FAILURES",
      'class="banner fail"' in widened
      and 'id="feature-source_feature" class="bad"' in widened)
check("a row that is both failing and unresolved is named once",
      widened.count("feature source_feature") == 1, widened)

clean_declaration = {"source": "clean", "version": "", "features": [], "types": [],
                     "storages": []}
clean_sample = {"registry": "Record", "columns": ["uid", "roi_uid"], "merged": ["roi_uid"],
                "rows": [["r1QrTz00", "ROI-0001"]]}
clean = schema_review.build(clean_declaration, landed, [clean_sample], real_kind, real_scalar)
clean_page = render.render(clean)
check("a confirmed and judged run renders green on both panels",
      'class="banner fail"' not in clean_page and 'class="banner warn"' not in clean_page
      and clean_page.count('class="banner pass"') == 2,
      str(clean_page.count('class="banner pass"')))
check("green claims a confirmation rather than an absence of news",
      "every declared name was confirmed to land as declared" in clean_page
      and "all 1 sampled values were judged and none was refused" in clean_page)

# An absent deprecated name is never a failure, and panel 1 does not claim it landed.
retired_only = schema_review.build(
    {"source": "retired", "version": "", "types": [], "storages": [],
     "features": [f for f in declaration["features"] if f["name"] == "age_at_itx"]},
    landed, [clean_sample], real_kind, real_scalar)
retired_gaps = schema_review.report(retired_only)
retired_page = render.render(retired_only)
retired_panel_one = retired_page.split("2. Sampled rows")[0]
check("the one accounting counts a deprecated name that did not land",
      [r["name"] for r in retired_gaps["retired"]] == ["age_at_itx"],
      str(retired_gaps["retired"]))
check("a deprecated name that did not land stops panel 1 reading green",
      'class="banner pass"' not in retired_panel_one
      and "1 deprecated name did not land" in retired_panel_one,
      retired_panel_one.split('class="banner')[1].split("</div>")[0])
check("a deprecated name that did not land is never a failure",
      'class="banner fail"' not in retired_page
      and not compare.failures(schema_review.schema_rows(retired_only)))
check("the verdict box names the deprecated names that did not land",
      "age_at_itx" in retired_page.split("3. Verdict")[1],
      retired_page.split("3. Verdict")[1][:600])
check("the stderr line counts them",
      "1 deprecated names absent" in schema_review.summarise(retired_only),
      schema_review.summarise(retired_only))

# One registry judged clean, one whose merged feature reached no column.
mixed_samples = json.loads((FIXTURES / "mixed_samples.json").read_text())
mixed = schema_review.build(clean_declaration, landed, mixed_samples, real_kind, real_scalar)
mixed_gaps = schema_review.report(mixed)
mixed_page = render.render(mixed)
panel_two = mixed_page.split("2. Sampled rows")[1]
check("the mixed case really is judged, clean and gapped",
      mixed_gaps["judged"] == 2 and not mixed_gaps["problems"] and not mixed_gaps["skipped"]
      and [(a["registry"], a["column"]) for a in mixed_gaps["absent_columns"]]
      == [("Collection", "cell_type")],
      str(mixed_gaps["absent_columns"]))
check("a merged feature that reached no column stops the panel reading green",
      'class="banner pass"' not in panel_two and 'class="banner warn"' in panel_two,
      panel_two.split('class="banner')[1].split("</div>")[0])
check("the banner keeps a gapped run apart from a clean one and from an unchecked one",
      "2 sampled values were judged and none was refused, and 1 merged feature column never "
      "reached the frame" in panel_two
      and "nothing on this panel was checked" not in panel_two, panel_two[:400])
check("the affected registry says so in its own heading",
      "1 rows, NO cell judged, 0 flagged, 0 unjudged, 1 merged features reached no column"
      in panel_two)
check("the registry that was judged cleanly keeps a clean heading",
      "2 rows, 2 cells judged, 0 flagged, 0 unjudged</span>" in panel_two)
check("a merged feature that reached no column is never reported as a failure",
      'class="banner fail"' not in panel_two)
check("the verdict box carries the gap the panel above it does",
      "<strong>1</strong> merged feature columns never reached a frame" in mixed_page
      and "No sampled cell was judged in" in mixed_page, mixed_page.split("3. Verdict")[1][:600])
check("the stderr line carries every count the page does",
      schema_review.summarise(mixed)
      == ("0 schema failures, 0 declared dtypes unresolved, 0 deprecated names absent, "
          "0 flagged values, 2 cells judged, "
          "0 left unjudged, 1 merged features with no column, 1 registries with no cell judged, "
          "2 registries sampled, 0 named registries not sampled"),
      schema_review.summarise(mixed))

# A registry whose sampled rows carry no feature value judges nothing.
featureless = {"registry": "Feature", "columns": ["uid", "name"], "merged": [],
               "rows": [["f1QrTz00", "age"], ["f2QrTz01", "n_cells"]]}
thin_run = schema_review.build(clean_declaration, landed, [clean_sample, featureless],
                               real_kind, real_scalar)
thin_gaps = schema_review.report(thin_run)
thin_page = render.render(thin_run)
check("a registry with nothing to merge records no gap of its own",
      not thin_gaps["absent_columns"] and not thin_gaps["skipped"]
      and thin_gaps["unjudged_registries"] == ["Feature"], str(thin_gaps))
check("a registry where no cell was judged says so on its own heading",
      "2 rows, NO cell judged, 0 flagged, 0 unjudged" in thin_page)
check("a registry where no cell was judged stops the panel reading green",
      'class="banner pass"' not in thin_page.split("2. Sampled rows")[1]
      and "1 sampled registry had no cell judged at all" in thin_page)
check("a registry where no cell was judged is still not a failure",
      'class="banner fail"' not in thin_page and not thin_gaps["problems"])

# A registry named but not sampled because of the cap is reported as not looked at.
capped_run = schema_review.build(clean_declaration, landed, [clean_sample], real_kind, real_scalar,
                                 ["Schema", "Run"])
capped_gaps = schema_review.report(capped_run)
capped_page = render.render(capped_run)
capped_panel_two = capped_page.split("2. Sampled rows")[1].split("3. Verdict")[0]
check("the one accounting records the registries the cap left unsampled",
      capped_gaps["dropped_registries"] == ["Schema", "Run"], str(capped_gaps))
check("an unsampled registry stops panel 2 reading green",
      'class="banner pass"' not in capped_panel_two
      and "2 named registries were not sampled at all" in capped_panel_two,
      capped_panel_two.split('class="banner')[1].split("</div>")[0])
check("panel 2 names the registries it did not sample",
      "Schema, Run" in capped_panel_two, capped_panel_two[:800])
check("the verdict box names them too",
      "Schema, Run" in capped_page.split("3. Verdict")[1])
check("the stderr line counts them",
      "2 named registries not sampled" in schema_review.summarise(capped_run),
      schema_review.summarise(capped_run))
check("an unsampled registry is never a failure",
      'class="banner fail"' not in capped_page and not capped_gaps["problems"])

# Every surface reads compare.gap_report: a mutated report must move their output.
settled_gap_report = compare.gap_report
real_gaps = schema_review.report(context)
summary_line = schema_review.summarise(context)
GHOST = "GhostRegistry"


def _mutated_gap_report(rows, samples_in, validated_in, dropped_in=None):
    report = settled_gap_report(rows, samples_in, validated_in, dropped_in)
    first = dict(report["registries"][0], judged=report["registries"][0]["judged"] + 7)
    return {
        **report,
        "judged": report["judged"] + 7,
        "looked_at": report["looked_at"] + 8,
        "skipped": report["skipped"] + [{"registry": GHOST, "row": 0, "identity": "ghost",
                                         "column": "ghost_feature", "value": "x",
                                         "reason": "a reason this test injected"}],
        "absent_columns": report["absent_columns"] + [{"registry": GHOST,
                                                       "column": "ghost_feature"}],
        "unresolved": report["unresolved"] + [dict(report["unresolved"][0], name="ghost_feature")],
        "retired": report["retired"] + [dict(report["retired"][0], name="ghost_retired")],
        "unjudged_registries": report["unjudged_registries"] + [GHOST],
        "dropped_registries": [GHOST],
        "registries": [first] + report["registries"][1:],
    }


compare.gap_report = _mutated_gap_report
try:
    moved_page = render.render(context)
    moved_line = schema_review.summarise(context)
finally:
    compare.gap_report = settled_gap_report

moved_panel_one = moved_page.split("2. Sampled rows")[0]
moved_panel_two = moved_page.split("2. Sampled rows")[1].split("3. Verdict")[0]
moved_verdict = moved_page.split("3. Verdict")[1]
check("panel 1's banner counts the unconfirmed names the one accounting reports",
      f"{len(real_gaps['unresolved']) + 1} could not be confirmed either way" in moved_panel_one,
      moved_panel_one.split('class="banner')[1].split("</div>")[0])
check("panel 1's banner counts the deprecated names the one accounting reports",
      f"{len(real_gaps['retired']) + 1} deprecated names did not land" in moved_panel_one,
      moved_panel_one.split('class="banner')[1].split("</div>")[0])
check("the verdict box names the deprecated names the one accounting reports",
      "ghost_retired" in moved_verdict, moved_verdict[:900])
check("panel 2's banner counts the unjudged cells the one accounting reports",
      f"{len(real_gaps['skipped']) + 1} were left unjudged" in moved_panel_two,
      moved_panel_two.split('class="banner')[1].split("</div>")[0])
check("a registry heading counts the judged cells the one accounting reports",
      f'{real_gaps["registries"][0]["judged"] + 7} cells judged' in moved_panel_two,
      moved_panel_two)
check("the verdict box counts what the one accounting reports",
      f"<strong>{len(real_gaps['unresolved']) + 1}</strong> declared dtypes" in moved_verdict
      and f"<strong>{len(real_gaps['skipped']) + 1}</strong> sampled cells" in moved_verdict
      and f"<strong>{real_gaps['looked_at'] + 8}</strong>" in moved_verdict
      and f"<strong>{len(real_gaps['absent_columns']) + 1}</strong> merged feature columns"
      in moved_verdict,
      moved_verdict[:900])
check("the stderr line counts what the one accounting reports",
      f"{len(real_gaps['retired']) + 1} deprecated names absent" in moved_line
      and f"{real_gaps['judged'] + 7} cells judged" in moved_line
      and f"{len(real_gaps['skipped']) + 1} left unjudged" in moved_line
      and f"{len(real_gaps['absent_columns']) + 1} merged features with no column" in moved_line
      and f"{len(real_gaps['unjudged_registries']) + 1} registries with no cell judged"
      in moved_line
      and "1 named registries not sampled" in moved_line,
      moved_line)
check("every surface that names a registry names the one the accounting named",
      GHOST in moved_panel_two and moved_verdict.count(GHOST) == 2,
      f"panel two {GHOST in moved_panel_two}, verdict {moved_verdict.count(GHOST)}")
check("the real accounting is restored, so nothing after this reads the mutation",
      schema_review.report(context)["dropped_registries"] == []
      and schema_review.summarise(context) == summary_line, schema_review.summarise(context))

# Every surface reads compare.schema_rows: an added row must reach the page and the command.
settled_schema_rows = compare.schema_rows
real_failures = len(compare.failures(schema_review.schema_rows(context)))


def _fourth_section(schema):
    return settled_schema_rows(schema) + [
        {"name": "ghost_section", "declared_dtype": "int", "expected_dtype": "int",
         "landed_dtype": "", "status": "active", "description": "",
         "verdict": compare.MISSING}]


compare.schema_rows = _fourth_section
try:
    fourth_page = render.render(context)
    fourth_line = schema_review.summarise(context)
finally:
    compare.schema_rows = settled_schema_rows
check("panel 1's banner counts every section the one row list carries",
      f"{real_failures + 1} declared names did not land as declared" in fourth_page,
      fourth_page.split('class="banner')[1].split("</div>")[0])
check("the verdict box and the stderr line count them too",
      f"<strong>{real_failures + 1}</strong> schema failures" in fourth_page
      and f"{real_failures + 1} schema failures" in fourth_line, fourth_line)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
