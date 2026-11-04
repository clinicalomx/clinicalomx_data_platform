"""Related values are reported, unasked, when an exact filter is added."""
import sys

import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/nmstore", name="nm")
import lamindb as ln

ln.Feature(name="qptiff_study_name", dtype=str).save()
ln.Feature(name="image_type", dtype=str).save()
# Two study names, one a prefix of the other.
rows = ([("QSBC", "h_and_e")] * 3 + [("QSBC", "multiplex")] * 2
        + [("QSBC Sample", "h_and_e")] * 4 + [("OTHER-STUDY", "h_and_e")] * 2)
for i, (study, kind) in enumerate(rows):
    p = f"/tmp/nm{i}.qptiff"; open(p, "w").write(f"slide {i}")
    ln.Artifact(p, key=f"img/nm{i}.qptiff").save().features.add_values(
        {"qptiff_study_name": study, "image_type": kind})

sys.path.insert(0, "/explorer")
import agent

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r} (want {want!r})")
    if not ok: fails.append(label)

q = agent.blank_query("Artifact")
seen, q, _ = agent._apply(q, "add_condition",
                          {"field": "qptiff_study_name", "lookup": "exact", "value": "QSBC"})

eq("the condition still applies", seen["matched"], 5)
eq("the related value is reported unasked",
   seen.get("other_values_of_this_field"), ["QSBC Sample"])
eq("and the note says what to do about it",
   'match "any"' in seen.get("note", ""), True)
eq("an unrelated study is NOT reported as related",
   "OTHER-STUDY" in seen.get("other_values_of_this_field", []), False)

# The longer name reports the shorter one too.
q2 = agent.blank_query("Artifact")
seen2, _, _ = agent._apply(q2, "add_condition", {"field": "qptiff_study_name",
                                                 "lookup": "exact", "value": "QSBC Sample"})
eq("the containment is checked both ways",
   seen2.get("other_values_of_this_field"), ["QSBC"])

# Acting on the hint gives the complete answer.
both = agent.blank_query("Artifact")
_, both, _ = agent._apply(both, "add_condition", {
    "field": "qptiff_study_name", "lookup": "in", "value": ["QSBC", "QSBC Sample"]})
_, both, _ = agent._apply(both, "add_condition", {
    "field": "image_type", "lookup": "exact", "value": "h_and_e"})
seen3, both, _ = agent._apply(both, "summarise", {"op": "count"})
eq("both studies together give the fuller answer", seen3["rows"], [["7"]])

# A value nobody has says so.
q4 = agent.blank_query("Artifact")
seen4, _, _ = agent._apply(q4, "add_condition", {"field": "qptiff_study_name",
                                                 "lookup": "exact", "value": "QSB"})
eq("a zero match is flagged", seen4["matched"], 0)
eq("with the near values to try instead",
   sorted(seen4.get("other_values_of_this_field", [])), ["QSBC", "QSBC Sample"])
eq("and a warning not to trust the zero", "nothing matched" in seen4.get("note", ""), True)

# Scoped to the rows before the condition.
q5 = agent.blank_query("Artifact")
_, q5, _ = agent._apply(q5, "add_condition", {"field": "image_type", "lookup": "exact",
                                              "value": "multiplex"})
seen5, _, _ = agent._apply(q5, "add_condition", {"field": "qptiff_study_name",
                                                 "lookup": "exact", "value": "QSBC"})
eq("no related value is reported when none is in scope",
   seen5.get("other_values_of_this_field"), None)

# A lookup that is already broad needs no hint.
q6 = agent.blank_query("Artifact")
seen6, _, _ = agent._apply(q6, "add_condition", {"field": "qptiff_study_name",
                                                 "lookup": "icontains", "value": "QSBC"})
eq("icontains gets no near-miss note", "other_values_of_this_field" in seen6, False)
eq("and it already spans both", seen6["matched"], 9)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
