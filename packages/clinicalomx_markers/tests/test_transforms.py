"""Mapping-claim transforms: library tools, determinism, one claim per field, previews, and
bespoke code kept for a human.

`fixtures/sheets/compound_panel.csv` mimics a vendor panel sheet. Expected counts are computed
here with the standard library, never by the code under test.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain import transform
from clinicalomx_markers.domain.reagent_mapping import (
    REAGENT_MAPPING_DOMAIN,
    NeedsAHuman,
    ReagentMappingPredicate,
    agent_may_accept,
)
from clinicalomx_markers.propose.columns import (
    DESTINATION,
    column_profile,
    map_columns,
    read_sheet,
    request_for,
)
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.schema import Attention
from clinicalomx_markers.sources.sheet import read_columns

P = ReagentMappingPredicate
SHEET = Path(__file__).parent / "fixtures" / "sheets" / "compound_panel.csv"
SUBJECT = "compound_panel.csv"

TARGET = 'before(" (", last=True)'
CLONE = 'between("(", ")", last=True)'
BARCODE = 'after("-", last=True)'


def _column(header: str):
    return next(c for c in read_columns(SHEET) if c.header == header)


def _file_rows(header: str) -> list[str]:
    with SHEET.open(newline="") as fh:
        return [row[header] for row in csv.DictReader(fh) if any(row.values())]


# -- the library ---------------------------------------------------------------------------

@pytest.mark.parametrize("chain, cell, expected", [
    (TARGET, "ASCT2 (AKYP0176)-BX074", "ASCT2"),
    (CLONE, "ASCT2 (AKYP0176)-BX074", "AKYP0176"),
    (BARCODE, "ASCT2 (AKYP0176)-BX074", "BX074"),
    (TARGET, "Histone H3 Phospho (Ser28) (AKYP0060)-BX030", "Histone H3 Phospho (Ser28)"),
    (CLONE, "Histone H3 Phospho (Ser28) (AKYP0060)-BX030", "AKYP0060"),
    ('between("(", ")")', "Histone H3 Phospho (Ser28) (AKYP0060)-BX030", "Ser28"),
    (BARCODE, "E-cadherin (AKYP0057)-BX014", "BX014"),
    ('split("-", -1)', "E-cadherin (AKYP0057)-BX014", "BX014"),
    ('split("-", 5)', "E-cadherin (AKYP0057)-BX014", ""),
    ('before(" ", last=True) | trim()', "Alexa Fluor 647 50000", "Alexa Fluor 647"),
    ("casefold()", "Alexa Fluor 647", "alexa fluor 647"),
    (CLONE, "Keratin 8/18-BX107", ""),
    ('pattern("-(BX[0-9]+)$")', "ASCT2 (AKYP0176)-BX074", "BX074"),
    ('pattern("AKYP[0-9]+")', "ASCT2 (AKYP0176)-BX074", "AKYP0176"),
])
def test_a_chain_reads_the_part_of_a_cell_it_names(chain, cell, expected):
    assert transform.run(transform.steps(chain), cell) == expected


def test_a_marker_that_is_not_there_gives_no_value_rather_than_the_whole_cell():
    """A missing marker gives "", never the whole cell."""
    for chain in (TARGET, CLONE, BARCODE, 'split("|", 1)'):
        assert transform.run(transform.steps(chain), "no markers here") == ""


def test_several_columns_join_into_one_field_through_the_same_model():
    reading = transform.parse('Sheet1!A1 + Sheet1!C1 | join(" ") | trim()')
    assert reading.apply(("BX074", "RX074")) == "BX074 RX074"
    assert reading.apply(("BX074", "")) == "BX074"
    assert reading.attention()[0] is Attention.REVIEW


@pytest.mark.parametrize("text", [
    "Sheet1!B1",
    'Sheet1!B1 | between("(", ")", last=True)',
    'Sheet1!B1 | between("(", ")", last=True) | trim()',
    'Sheet1!B1 | split(" | ", -1)',
    'Sheet1!B1 | pattern("\\\\((AKYP[0-9]+)\\\\)")',
    'My Sheet!AA1 + My Sheet!AB1 | join(", ")',
])
def test_a_reading_is_its_own_text(text):
    reading = transform.parse(text)
    assert transform.parse(reading.text()) == reading
    assert reading.text() == text


@pytest.mark.parametrize("text, says", [
    ('Sheet1!B1 | upper()', "not a transform tool"),
    ('Sheet1!B1 | __import__("os")', "not a transform tool"),
    ('Sheet1!B1 | after(open("x").read())', "every argument is a string"),
    ('Sheet1!B1 | after(*["-"])', "every argument is a string"),
    ('Sheet1!B1 | after(**{"marker": "-"})', "no **arguments"),
    ('Sheet1!B1 | after("")', "blank"),
    ('Sheet1!B1 | after(1)', "marker is a str"),
    ('Sheet1!B1 | split("-", "1")', "index is a int"),
    ('Sheet1!B1 | after("-", last=1)', "last is a bool"),
    ('Sheet1!B1 | after("-", "x", "y", "z")', "after()"),
    ('Sheet1!B1 | pattern("(a)(b)")', "at most one group"),
    ('Sheet1!B1 | pattern("[")', "not a regular expression"),
    (f'Sheet1!B1 | pattern("{"a" * 201}")', "over the 200"),
    ('Sheet1!B1 | pattern("(\\\\w+\\\\s?)+\\\\)")', "a repeated group holds another repeat"),
    ('Sheet1!B1 | pattern("(?:a|b+)*c")', "a repeated group holds another repeat"),
    ('Sheet1!B1 | join(" ")', "reads one column"),
    ('Sheet1!A1 + Sheet1!B1 | trim()', "first join"),
    ('Reporter Barcode | trim()', "column addresses"),
    ('Sheet1!B1 | after("-', "unterminated"),
])
def test_what_is_not_a_library_step_is_refused_and_never_evaluated(text, says):
    with pytest.raises(ValueError, match=re.escape(says)):
        transform.parse(text)


def test_attention_follows_what_a_reviewer_audits():
    assert transform.parse(f"Sheet1!B1 | {CLONE}").attention()[0] is Attention.SKIM
    assert transform.parse(f"Sheet1!B1 | {CLONE} | trim()").attention()[0] is Attention.REVIEW
    written = transform.parse('Sheet1!B1 | pattern("AKYP[0-9]+")')
    assert written.bespoke
    assert written.attention()[0] is Attention.SCRUTINISE
    assert "newly written code" in written.attention()[1]


# -- determinism ---------------------------------------------------------------------------

_RUN = """
import json, sys
from clinicalomx_markers.domain import transform
from clinicalomx_markers.sources.sheet import read_columns
column = next(c for c in read_columns(sys.argv[1]) if c.header == "Panel Antibody")
chains = json.loads(sys.argv[2])
print(json.dumps({c: [transform.run(transform.steps(c), v) for v in column.values]
                  for c in chains}))
"""


def test_a_chain_gives_the_same_answer_in_separate_processes():
    """Two fresh interpreters with different hash seeds give the same answer."""
    chains = json.dumps([TARGET, CLONE, BARCODE, 'pattern("-(BX[0-9]+)$") | casefold()'])
    outputs = []
    for seed in ("1", "2"):
        done = subprocess.run([sys.executable, "-c", _RUN, str(SHEET), chains],
                              capture_output=True, text=True, check=True,
                              env={**os.environ, "PYTHONHASHSEED": seed})
        outputs.append(json.loads(done.stdout))
    assert outputs[0] == outputs[1]
    assert outputs[0][CLONE][0] == "AKYP0176"


# -- one column, several independent claims ------------------------------------------------

def _three_claims():
    column = _column("Panel Antibody")
    person = HumanProposer(by="rafael")
    for field, chain in ((P.TARGET_LABEL, TARGET), (P.CLONE, CLONE), (P.BX_BARCODE, BARCODE)):
        person.answer(request_for(column), field, confidence=0.9, predicate=DESTINATION,
                      transform=chain)
    return map_columns(SUBJECT, [column], proposer=person, source="human")


def test_one_column_produces_one_claim_per_field_it_fills():
    mapping = _three_claims()
    assert {(c.predicate, c.value) for c in mapping.claims} == {
        (P.TARGET_LABEL, f"Sheet1!B1 | {TARGET}".replace("Sheet1", "compound_panel")),
        (P.CLONE, f"compound_panel!B1 | {CLONE}"),
        (P.BX_BARCODE, f"compound_panel!B1 | {BARCODE}"),
    }
    assert len({c.claim_id() for c in mapping.claims}) == 3
    assert not mapping.unplaced


def test_rejecting_one_claim_from_a_column_leaves_the_others_standing():
    mapping = _three_claims()
    store = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={SUBJECT: "Artifact"})
    by = {c.predicate: store.add(c) for c in mapping.claims}
    signed = Judgement(by="rafael", at="2026-09-25", reason="read off the cell")
    store.decide(by[P.TARGET_LABEL], outcome="accepted", judgement=signed)
    store.decide(by[P.CLONE], outcome="accepted", judgement=signed)
    store.decide(by[P.BX_BARCODE], outcome="rejected",
                 judgement=Judgement(by="rafael", at="2026-09-25", kind="one_off",
                                     reason="column A already supplies the barcode"))

    answered = {d.predicate for d in store.answers(SUBJECT)}
    assert answered == {P.TARGET_LABEL, P.CLONE}


def test_a_value_naming_a_transform_that_does_not_run_is_refused_at_write():
    store = MemoryClaimStore(REAGENT_MAPPING_DOMAIN, subject_types={SUBJECT: "Artifact"})
    claim = REAGENT_MAPPING_DOMAIN.claim(
        subject=SUBJECT, predicate=P.CLONE, value="compound_panel!B1 | inside()",
        source="human", source_id="compound_panel!B1", evidence="header 'Panel Antibody'")
    with pytest.raises(ValueError, match="not a transform tool"):
        store.add(claim)
    assert store.held(SUBJECT) == []


# -- a bespoke transform and policy --------------------------------------------------------

def test_an_agent_may_accept_a_library_chain_and_never_newly_written_code():
    """An agent may accept a library chain, never a bespoke pattern, even under an approved
    policy."""
    agent_may_accept(P.CLONE, f"compound_panel!B1 | {CLONE} | trim()", {})
    agent_may_accept(P.RX_BARCODE, "compound_panel!C1", {})
    with pytest.raises(NeedsAHuman, match="only a human accepts one"):
        agent_may_accept(P.CLONE, 'compound_panel!B1 | pattern("AKYP[0-9]+")', {})


# -- the preview ---------------------------------------------------------------------------

def test_the_preview_count_is_what_the_transform_produces_on_the_sheet():
    column = _column("Panel Antibody")
    reading = transform.reading((column.address,), CLONE)
    shown = transform.preview(reading, ((v,) for v in column.values))

    cells = _file_rows("Panel Antibody")
    produced = sum(1 for v in cells if "(" in v and ")" in v.rsplit("(", 1)[1])
    assert (shown.produced, shown.rows) == (produced, len(cells)) == (9, 11)
    assert shown.examples[0] == ("Keratin 8/18-BX107", ""), "a row that gave nothing leads"
    assert ("ASCT2 (AKYP0176)-BX074", "AKYP0176") in shown.examples


def test_a_preview_from_the_stored_profile_matches_one_from_the_rows():
    """A preview from the stored column profile matches one from the rows."""
    column = _column("Panel Antibody")
    profile = column_profile(column)
    for chain in (TARGET, CLONE, BARCODE, 'pattern("Ser[0-9]+")'):
        reading = transform.reading((column.address,), chain)
        rows = transform.preview(reading, ((v,) for v in column.values))
        counted = transform.preview_counted(
            reading, (((c["value"],), c["count"]) for c in profile["cells"]))
        assert (counted.produced, counted.rows) == (rows.produced, rows.rows)
    assert profile["cells_complete"]


# -- the rule proposer ---------------------------------------------------------------------

def test_the_rule_proposer_reads_a_barcode_out_of_a_compound_column_and_says_it_repeats_one():
    mapping = read_sheet(SHEET)
    [claim] = [c for c in mapping.claims if c.source_id.endswith("!B1")]
    assert claim.predicate == P.BX_BARCODE
    assert claim.value == f"compound_panel!B1 | {BARCODE}"
    assert "10/10 filled cells give a value" in claim.evidence

    [proposal] = [p for c, p in mapping.proposals if c.header == "Panel Antibody"]
    held = [r for r in proposal.reasons if r.outcome == "held back"]
    assert held and "compound_panel!A1" in held[0].evidence
    assert proposal.triage.label is Attention.SCRUTINISE


def test_a_column_the_whole_cell_already_fits_gets_no_extraction():
    mapping = read_sheet(SHEET)
    assert [c.value for c in mapping.claims if c.predicate == P.RX_BARCODE] == [
        "compound_panel!C1"]


# -- the library grows from the work -------------------------------------------------------

def test_usage_counts_each_accepted_chain_and_its_sources():
    accepted = [
        ("panel-a.xlsx", f"Sheet1!B1 | {CLONE}"),
        ("panel-b.xlsx", f"Sheet1!D1 | {CLONE}"),
        ("panel-b.xlsx", 'Sheet1!B1 | pattern("AKYP[0-9]+")'),
        ("panel-c.xlsx", 'Sheet1!E1 | pattern("AKYP[0-9]+")'),
        ("panel-c.xlsx", "Sheet1!A1"),
        ("panel-c.xlsx", "Reporter Barcode"),
    ]
    by = {u.chain: u for u in transform.usage(accepted)}
    assert (by[CLONE].accepted, by[CLONE].sources) == (2, ("panel-a.xlsx", "panel-b.xlsx"))
    assert not by[CLONE].promotion_candidate, "a library chain is already in the library"
    written = by['pattern("AKYP[0-9]+")']
    assert written.bespoke and written.promotion_candidate
    assert by["identity()"].accepted == 1
    assert "Reporter Barcode" not in by


def test_a_transform_raises_a_triage_and_never_lowers_or_restates_it():
    from clinicalomx_markers.propose.columns import raised
    from clinicalomx_markers.propose.schema import Triage

    sure = Triage(Attention.SKIM, "confidence 0.99")
    unsure = Triage(Attention.SCRUTINISE, "confidence 0.25")
    assert raised(sure, "") is sure, "the whole cell asks nothing more"
    assert raised(unsure, CLONE) is unsure
    assert raised(sure, f"{CLONE} | trim()").label is Attention.REVIEW
    written = raised(sure, 'pattern("AKYP[0-9]+")')
    assert written.label is Attention.SCRUTINISE
    assert written.reason.startswith("the transform holds newly written code")


def test_the_chain_of_no_steps_reads_back_as_no_steps():
    assert transform.parse("Sheet1!E1").chain == transform.IDENTITY
    assert transform.steps(transform.IDENTITY) == ()


def test_a_catastrophic_pattern_is_refused_before_it_can_run():
    catastrophic = r"(\w+\s?)+\)"
    with pytest.raises(ValueError, match="a repeated group holds another repeat"):
        transform.Step.of("pattern", catastrophic)
    assert transform.Step.of("pattern", r"-(BX\d+)?$").args == (r"-(BX\d+)?$",)


@pytest.mark.parametrize("columns", [
    ("IO60 + Met!B1",),
    ("a|b!C1",),
    ("'quoted!D1",),
    ("IO60 + Met!A1", "Sheet1!C1"),
])
def test_a_sheet_name_holding_a_separator_reads_back_as_itself(columns):
    steps = ('join(" ") | trim()',) if len(columns) > 1 else ('after("-", last=True)',)
    written = transform.reading(columns, *steps)
    assert transform.parse(written.text()) == written


def test_a_plain_sheet_is_spelled_as_it_always_was():
    assert transform.reading(("Sheet1!B1",), "trim()").text() == "Sheet1!B1 | trim()"
    assert transform.parse("Sheet1!A1 + Sheet1!C1 | join(\" \")").columns == (
        "Sheet1!A1", "Sheet1!C1")
