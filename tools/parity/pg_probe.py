"""Stage 2 LaminDB read-path parity probe. Runs inside a disposable Postgres-backed
instance created by pg_driver.py. Every check prints PASS/FAIL/INFO; exit 1 on any FAIL."""
import pathlib
import sys
import tempfile

results = []


def check(name, fn, required=True):
    """Run `fn`, record PASS, or FAIL when required and INFO when not. Never raises."""
    try:
        detail = fn()
        results.append(("PASS", name, detail or ""))
    except Exception as e:  # noqa: BLE001 - evidence capture, every failure recorded
        results.append(("FAIL" if required else "INFO", name, f"{type(e).__name__}: {e}"))


import lamindb as ln
import clinicalomx
from clinicalomx import features as cx_features
from clinicalomx import review as cx_review


def p1():
    """The instance is Postgres-backed, not SQLite. Every later probe depends on this."""
    db = ln.setup.settings.instance.db
    assert db.startswith("postgresql"), db
    return f"backend={db.split('://', 1)[0]}, lamindb={ln.__version__}"


check("P1 instance is Postgres-backed, lamindb version", p1)


def p2():
    """Types and records round-trip, and a second create returns the first record."""
    clinicalomx.ensure_types({"TMA": "tissue microarray", "TMA core": "one core"})
    t = clinicalomx.get_type("TMA")
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    again = clinicalomx.get_or_create("TMA1", "TMA")
    assert roi.uid == again.uid, "get_or_create not idempotent"
    return f"roi uid={roi.uid}"


check("P2 ensure_types / get_type / get_or_create idempotent roundtrip", p2)

tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "a.txt").write_text("payload-a")
(tmp / "b.txt").write_text("payload-b")


def p3():
    """Annotating writes both directions, and a re-run changes nothing."""
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    art = ln.Artifact(tmp / "a.txt", key="parity/a.txt").save()
    clinicalomx.annotate(roi, art, "segmentations")
    clinicalomx.annotate(roi, art, "segmentations")  # idempotent re-run
    vals = art.features.get_values()
    assert vals["roi_uid"] == roi.uid, vals
    assert vals["roi_name"] == "TMA1", vals
    assert vals["roi_type"] == "TMA", vals
    return "back-refs roi_uid/roi_name/roi_type read back exact"


check("P3 annotate writes both directions; back-refs read back", p3)


def p4():
    """An artifact resolves back to the ROI that annotated it."""
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    art = ln.Artifact.get(key="parity/a.txt")
    resolved = clinicalomx.require_roi(art)
    assert resolved.uid == roi.uid
    return "require_roi resolves the annotated ROI"


check("P4 resolve_roi / require_roi read path", p4)


def p5():
    """A string holding `, ` reads back whole. It does not on SQLite: that comma splits it."""
    # measured SQLite fork: a str value containing ", " reads back wrong on SQLite
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    clinicalomx.ensure_features({"scanner_note": str})
    wanted = "Vectra Polaris, batch 7, re-scan"
    cx_features.upsert_values(roi.features, {"scanner_note": wanted})
    got = roi.features.get_values()["scanner_note"]
    assert got == wanted, f"read back {got!r}"
    return f"comma-bearing str reads back exact: {got!r}"


check("P5 str value containing ', ' reads back intact (known SQLite fork)", p5)


def p6():
    """An artifact-valued feature reads back as artifacts, newest last."""
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    art_b = ln.Artifact(tmp / "b.txt", key="parity/b.txt").save()
    clinicalomx.annotate(roi, art_b, "segmentations")
    val = roi.features.get_values()["segmentations"]
    arts = clinicalomx.all_artifacts(val)
    assert len(arts) == 2, f"expected 2 artifacts, got {len(arts)}"
    newest = clinicalomx.newest_artifact(val)
    assert newest is not None
    one = clinicalomx.as_artifact(clinicalomx.all_artifacts(val)[0])
    assert one is not None
    return f"all_artifacts=2, newest={newest.key}"


check("P6 artifact-valued feature reads: all_artifacts / newest_artifact", p6)


def p7():
    """The review gate narrows in SQL. States are Record state, so the queryset path must work."""
    # review states are Record state by design (review.py: "HITL STOPs as durable
    # queryable Record state"); the SQL narrowing path expects a Record queryset
    cx_review.ensure_state_feature()
    roi = clinicalomx.get_or_create("TMA1", "TMA")
    clinicalomx.set_state(roi, "pending_review")
    assert clinicalomx.get_state(roi) == "pending_review"
    hits = clinicalomx.list_by_state(ln.Record.filter(), "pending_review")
    assert any(h.uid == roi.uid for h in hits), "SQL narrowing missed the record"
    clinicalomx.confirm(roi, "approved")  # no emitter registered: state-only advance
    assert clinicalomx.get_state(roi) == "approved"
    return "set_state -> get_state -> list_by_state(SQL) -> confirm all agree on a Record"


check("P7 review-gate state machine on Postgres (SQL narrowing path)", p7)


def p8():
    """Materialising twice builds once. Not required: a miss costs time, not correctness."""
    (tmp / "mat.txt").write_text("built-once")

    def compute():
        return {"parity/mat.txt": ln.Artifact(tmp / "mat.txt", key="parity/mat.txt").save()}

    def refuse():
        raise AssertionError("rebuilt despite existing artifact")

    first = clinicalomx.materialize(["parity/mat.txt"], compute=compute)
    again = clinicalomx.materialize(["parity/mat.txt"], compute=refuse)
    assert first["parity/mat.txt"].uid == again["parity/mat.txt"].uid
    return "materialize computed once, skip-path reused the record"


check("P8 idempotent materialize / existing_artifact", p8, required=False)


def p9():
    """Postgres refuses a type cycle with a trigger. SQLite accepts one, so this is a known fork."""
    # measured fork: type cycles accepted on SQLite, refused by a trigger on Postgres
    a = ln.Record(name="cycle-a", is_type=True).save()
    b = ln.Record(name="cycle-b", type=a, is_type=True).save()
    try:
        a.type = b
        a.save()
    except Exception as e:  # noqa: BLE001
        return f"cycle refused by backend: {type(e).__name__}"
    raise AssertionError("type cycle was ACCEPTED on Postgres")


check("P9 Record type cycle refused (Postgres trigger, known fork)", p9, required=False)

fails = 0
for status, name, detail in results:
    print(f"{status}  {name}  |  {detail}")
    fails += status == "FAIL"
sys.exit(1 if fails else 0)
