# How to use clinicalomx_markers

Task-by-task recipes for using the marker registry.
The reading half needs no model, no network and no database, so almost everything here runs after a plain `uv sync`.

## The five words you need

- **Marker**: the lab's canonical name for an analyte, spelled once: `NAKATPASE`, not `Na-K-ATPase`.
- **Alias**: any other spelling that resolves to a marker.
- **Claim**: one assertion, with the source it came from: "UniProt P05023 says Cell membrane".
- **Decision**: one ruling the lab made on one claim, accepted or rejected: "curator accepted cyto_structural on 2026-08-31".
- **Segmentation role**: what an imaging channel is good for: `nucleus`, `cyto_structural`, `marker` or `exclude`.

A claim is what a source SAYS.
A decision is what the lab DECIDED.
The answer is every claim whose newest decision accepts it, and a claim with no decision yet is in the review queue.
Two different questions, so two different functions answer them.

Two more terms:

- **Release export**: a JSON file holding the accepted claims at one moment. Readers use it instead of a database.
- **Claim store**: where claims and decisions are kept. `MemoryClaimStore` is in memory, for examples and tests; `LedgerClaimStore` is the same interface against a lamin instance.

## Set the package up

```bash
cd packages/clinicalomx_markers
uv sync
uv run pytest -q
```

`uv sync` resolves `clinicalomx` from its path in this repository, so it needs no credential.

On the WSL box, `uv run pytest -q` segfaults with exit 139 and no output.
Run `uv run pytest -q -p no:capture` there.
The cause is `import readline` inside pytest's capture plugin, which dumps core on that machine.

## Turn a raw channel name into a marker

Do this first, always.
Never match a raw panel name against a role table.

Aliases are accepted claims, and resolution reads them from a release export, so it needs no database.
The output below is from an export in which the alias `Ecad` for `E-CADHERIN` is accepted:

```python
from clinicalomx_ledger.grammar.published import load

from clinicalomx_markers.resolve import resolve, resolve_all

release = load("marker-rel-....json")
resolved, unresolved = resolve_all(["DAPI", "NakATPase", "Ecad", "pNRFR2"], release)
for r in resolved:
    print(f"{r.raw} -> {r.canonical} ({r.via})")
print("unresolved:", [u.raw for u in unresolved])
```

```text
DAPI -> DAPI (exact)
NakATPase -> NAKATPASE (key)
Ecad -> E-CADHERIN (alias)
unresolved: ['pNRFR2']
```

`via` says which step answered:

- `exact`: the name is a canonical name, spelled as declared.
- `key`: it matched a canonical name once case and separators were dropped. So `NakATPase`, `Na-K-ATPase` and `NAKATPASE` are one marker with no alias row.
- `alias`: an alias claim accepted when that release was cut.

Rules that follow:

- A rejected or undecided alias is not in the export. One accepted after the release waits for the next release, so one file always resolves the same way.
- Without a release, only canonical names resolve.
- A spelling accepted for two different markers resolves to neither.
- An unresolved name is never guessed. A consumer drops it, and it belongs in the human review queue.

`python -m clinicalomx_markers.curate.session` resolves the same way.
It reads the file `--release` names, or else the newest marker export (`marker-*.json`, by `released_at`) in `--exports`, which defaults to the current directory.

## Pick segmentation channels for a panel

Segmentation reads roles from a release export, so it needs no database.

1. On a connected instance, an operator cuts the release and writes the export once:

   ```python
   from pathlib import Path

   from clinicalomx_ledger import services

   release = services.cut_release(domain="marker", by="curator", actor_kind="human")
   Path(f"marker-{release.uid}.json").write_bytes(services.export_release(release.uid))
   ```

2. A consumer loads that file anywhere.
   The output below is from an export in which every `channels.csv` role and the `Ecad` alias are accepted:

   ```python
   from clinicalomx_ledger.grammar.published import load

   from clinicalomx_markers.query.segmentation import nuclear, structural

   release = load("marker-rel-....json")
   panel = ["DAPI", "NakATPase", "Ecad", "Vimentin", "CD8", "Citrate Synthase"]

   print("nuclear:   ", [c.canonical for c in nuclear(panel, release)])
   print("structural:", [c.canonical for c in structural(panel, release)])
   ```

   ```text
   nuclear:    ['DAPI']
   structural: ['NAKATPASE', 'E-CADHERIN', 'VIMENTIN']
   ```

Two channels were dropped, for two different reasons.
`candidates()` shows both:

```python
from clinicalomx_markers.query.segmentation import candidates
from clinicalomx_markers.resolve import resolve

print(resolve("CD8", release))
for c in candidates(panel, release):
    print(c.canonical, c.segmentation_role)
```

```text
Unresolved(raw='CD8', key='cd8')
DAPI nucleus
NAKATPASE cyto_structural
E-CADHERIN cyto_structural
VIMENTIN cyto_structural
CITRATE_SYNTHASE marker
```

- `CD8` never resolves: nothing maps it to `CD8A`. That is a queue item for a human, and an accepted `alias` claim in the next release fixes it.
- `CITRATE_SYNTHASE` resolves, then its role excludes it. It is mitochondrial, so it is punctate rather than an outline. `marker` means "real marker, not a morphology channel".

A marker with no accepted role in the release is dropped the same way.
A rejected or undecided role is never in an export, so it can never reach a mask.

This returns the candidate pool only.
Which candidate a run uses is an image-metrics decision, not the registry's.

**Do not hardcode a channel list in a pipeline.**
That is the bug this registry replaces.

## Ask what the lab decided, and what a source said

Two questions, two functions.
Both take rows a claim store already gave you, so neither reaches a database.

```python
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import Outcome
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerClaim, MarkerPredicate
from clinicalomx_markers.query.claims import asserted, settled, value_of

store = MemoryClaimStore(domain=MARKER_DOMAIN)
store.add(
    MarkerClaim(
        subject="NAKATPASE",
        predicate=MarkerPredicate.SEGMENTATION_ROLE,
        value="cyto_structural",
        source="uniprot",
        source_id="P05023",
        source_version="2026_02",
        evidence="Cell membrane. Melanosome.",
    ),
    outcome=Outcome.ACCEPTED,
    judgement=Judgement(by="curator", at="2026-08-31", reason="single-pass membrane"),
)

answers = store.answers("NAKATPASE")
print("settled: ", [(d.value, str(d.outcome)) for d in settled(answers, "NAKATPASE")])
print("value_of:", value_of(answers, "NAKATPASE", "segmentation_role").value)
said = asserted(store.held("NAKATPASE"), "NAKATPASE")["segmentation_role"]
print("asserted:", [(c.value, c.source) for c in said])
```

```text
settled:  [('cyto_structural', 'accepted')]
value_of: cyto_structural
asserted: [('cyto_structural', <MarkerSource.UNIPROT: 'uniprot'>)]
```

Rules a consumer must not work around:

- `settled` holds accepted decisions only, and no policy widens it. Read an undecided claim with `asserted`, and say so in whatever you render.
- A rejected claim is never in the answer. It is a known-wrong statement about biology.
- A predicate no human reviews never gets a decision. `subcellular_location` is read straight from a UniProt snapshot, so `settled` never answers it and `asserted` is the function to call.
- `settled` can hold several claims for one predicate, which is what an alias is. A single-valued consumer calls `value_of`, which returns the newest accepted decision, so a correction wins the moment it is accepted.

A segmentation config cannot hedge the way prose can, so an unvalidated role there is worse than a missing one.

## Cite an answer

For an agent that must say where a fact came from:

```python
from clinicalomx_markers.query.claims import line, supporting

claims = store.held("NAKATPASE", history=True)   # the history, not the projection
decided = store.decisions("NAKATPASE")[-1]

print(line(decided, supporting(decided, claims)))
```

```text
NAKATPASE segmentation_role = 'cyto_structural'
    accepted  curator 2026-08-31
    supported by [NAKATPASE:segmentation_role:uniprot:P05023:7e63416ddb18] uniprot P05023 @2026_02
```

The three lines say what the lab holds, who decided it, and on whose authority.

- `query.claims.marker(claim)` is the short inline form, `[NAKATPASE:segmentation_role:uniprot:P05023:7e63416ddb18]`. It ends in a digest, not the value, so it stays quotable whatever the claim says.
- Pass `store.held(subject, history=True)`, not `store.held(subject)`. A decision rests on the claims as they stood when it was made; without the history, a decision the evidence has since moved past looks unsupported.
- For a predicate nobody reviews, use `query.claims.claim_line(claim)`. It renders `UNDECIDED` rather than borrowing a status.

## Look up the reagent behind a marker

An antibody carries a `BX` barcode and its reporter carries the complementary `RX` barcode.
Equal numbers mean complementary oligos, so the pairing is derived, never stored.

```python
from clinicalomx_reagents.inventory import pairs, parse_barcode, reporter_for

print(pairs("BX002", "RX002"), pairs("BX002", "RX003"))
print(parse_barcode("bx2"))
print(reporter_for("BX002"))
```

```text
True False
BX002
{'rx_barcode': 'RX002', 'akoya_id': 'AKY-002', 'fluorophore': 'ALEXA_FLUOR_647', 'status': 'draft'}
```

Case and leading zeros do not matter, so `bx2` and `BX002` are one barcode.
That is the safe direction: two spellings of one key collide during a dedup rather than passing as two reagents.

These functions and the inventory belong to `clinicalomx_reagents`.
Every row in the shipped inventory is illustrative; the real inventory has not been imported yet.

## Check an inventory spreadsheet before importing it

`conjugate_problems` reports every fault at once, and takes rows you pass in, so a candidate is checked before anything is written:

```python
from clinicalomx_markers.domain.reagent_inventory import conjugate_problems

candidate = [{"catalogue_number": "ACME-1", "bx_barcode": "BX404",
              "canonical_name": "NOT_A_MARKER", "akoya_id": "NA", "uniprot_isoform": ""}]
for problem in conjugate_problems(antibodies=candidate):
    print("-", problem)
```

```text
- antibodies.csv: missing column 'vendor'
- antibodies.csv: missing column 'target_label'
- antibodies.csv: missing column 'status'
- antibodies.csv: ACME-1 names unknown marker 'NOT_A_MARKER'
```

`validate_conjugates` runs the same checks as a gate: it raises instead of returning a list.
Every check belongs to `clinicalomx_reagents.inventory` except two that need this package: the marker an antibody names, and an isoform's parent accession against that marker's target claims.

## Read several fields out of one spreadsheet column

A mapping claim's value is the column plus the transform its cells go through.
So one column filling three fields is three claims, each reviewed on its own.
A transform is a chain of named tools from `domain.transform.TOOLS`, run with no model, so a cell always gives the same answer.

```python
from clinicalomx_markers.domain import transform
from clinicalomx_markers.propose.columns import DESTINATION, map_columns, request_for
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.sources.sheet import columns_of

[column] = columns_of([["Panel Antibody"], ["ASCT2 (AKYP0176)-BX074"],
                       ["Histone H3 Phospho (Ser28) (AKYP0060)-BX030"]])
person = HumanProposer(by="curator")
for field, chain in (("target_label", 'before(" (", last=True)'),
                     ("clone", 'between("(", ")", last=True)'),
                     ("bx_barcode", 'after("-", last=True)')):
    person.answer(request_for(column), field, confidence=0.9, predicate=DESTINATION,
                  transform=chain)
mapping = map_columns("panel.xlsx", [column], proposer=person, source="human")
for claim in mapping.claims:
    reading = transform.parse(claim.value)
    shown = transform.preview(reading, ((v,) for v in column.values))
    print(f"{claim.predicate:<13} {shown.attention.value:<6} {shown.produced}/{shown.rows}",
          [out for _, out in shown.examples])
```

```text
target_label  skim   2/2 ['ASCT2', 'Histone H3 Phospho (Ser28)']
clone         skim   2/2 ['AKYP0176', 'AKYP0060']
bx_barcode    skim   2/2 ['BX074', 'BX030']
```

- `last=True` keeps the bracket inside `Histone H3 Phospho (Ser28)` in the target. Without it, `between("(", ")")` would read `Ser28` as the clone.
- To combine several columns into one field, start with `join`, such as `Sheet1!A1 + Sheet1!C1 | join(" ")`.
- When no tool fits, the step is `pattern(r"...")`. It is always `scrutinise`, and no agent may accept it under any policy.

## Add a marker or an alias

Seed data is hand-edited CSV under `src/clinicalomx_markers/seed_data/`, reviewed as a diff.

1. Edit the CSV: `markers.csv` for a new analyte.
2. Run the suite:

   ```bash
   uv run pytest -q -p no:capture
   ```

   `tests/test_registry.py` checks the seed set is consistent, and `tests/test_seed_spec.py` checks it against the column contract.

An alias is not seed data.
It is a `(marker, alias, spelling)` claim, proposed by `propose/alias.py` from UniProt synonyms or by a person through `propose/human.py`, then accepted or rejected like any claim.
It reaches resolution when the next release is cut and exported.

Only a spelling that key collapse cannot fix needs an alias:

- `Ecad` does, because collapsing gives `ecad` against `ecadherin`.
- `NakATPase` does not, because collapse already answers it, and a canonical name always wins over an alias.

Every row ships as `status=draft`, and `test_nothing_is_active_yet` fails the day that changes.
That is deliberate: activating a row should be a decision somebody makes.

To add, rename or remove a COLUMN, see `docs/maintaining.md`.

## Propose a segmentation role from a UniProt record

This is the authoring half, and the only place a model may run.
It runs offline against the seven captured UniProt fixtures.

```python
from pathlib import Path

from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.model import Outcome
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.curate.session import record_role
from clinicalomx_markers.domain.marker import MARKER_DOMAIN
from clinicalomx_markers.propose.role import RuleBasedRoleProposer
from clinicalomx_markers.propose.schema import ProposalRequest
from clinicalomx_markers.sources import snapshot, uniprot
from tests.conftest import fixture_transport

transport = fixture_transport()          # swap for snapshot.http to fetch live
record, fetched = uniprot.fetch("P05023", transport=transport)

snap = snapshot.capture(
    Path("/tmp/markers-snapshots"),
    source="uniprot", source_id="P05023", retrieved_at="2026-08-31",
    fetch=lambda: fetched,
)
print("snapshot: ", snap.key)
print("locations:", record.locations)

request = ProposalRequest(marker="NAKATPASE", evidence=record.evidence,
                          locations=record.locations, go_cc=record.go_cc)
proposal = RuleBasedRoleProposer().propose(request)[0]
print("proposed: ", proposal.value, "-", proposal.reasoning)
print("confidence:", proposal.confidence, "then",
      [(str(a.value), a.confidence) for a in proposal.alternatives])
print("triage:   ", proposal.triage.label.value, "-", proposal.triage.reason)

store = MemoryClaimStore(domain=MARKER_DOMAIN)
claim = record_role(store, snap, record, marker="NAKATPASE", role=proposal.value,
                    judgement=Judgement(by="curator", at="2026-08-31", reason="agreed"),
                    outcome=Outcome.ACCEPTED)
print("claim id: ", claim.claim_id())
print("queue:    ", store.in_state("unresolved"))
```

Run it from the package root, because it imports `tests.conftest` by name:

```bash
PYTHONPATH=. uv run python your_script.py
```

```text
snapshot:  clinicalomx_markers/snapshots/uniprot/P05023@2026-08-31.json
locations: ('Cell membrane', 'Basolateral cell membrane', 'Cell membrane, sarcolemma', 'Cell projection, axon', 'Melanosome')
proposed:  cyto_structural - localises to cell membrane, so it outlines the cell
confidence: 0.65 then [('marker', 0.3), ('nucleus', 0.0), ('exclude', 0.0)]
triage:    review - confidence 0.65 against 'marker' at 0.30
claim id:  NAKATPASE:segmentation_role:uniprot:P05023:7e63416ddb18
queue:     []
```

- `snapshot.capture` fetches only on a miss, so a re-run costs nothing and reads exactly what the first one saw. Two attempts then differ only in their reasoning.
- `RuleBasedRoleProposer` is the deterministic baseline.
- `LLMRoleProposer` in `propose/llm.py` implements the same interface. It needs the `[agent]` extra and a served model. Import it by its module, never through the package.

## Record what a person decided

A person proposes through the same interface a model does.
The curation recipe requires it: an agent is one actor behind a contract, never the only path.

```python
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.schema import Candidate, ProposalRequest

request = ProposalRequest(
    marker="CD8A",
    evidence="[Isoform 1]: Cell membrane. [Isoform 2]: Secreted",
    locations=("Cell membrane", "Secreted"),
)

me = HumanProposer(by="curator")
me.answer(request, "marker", confidence=0.9,
          reasoning="T cells only, so it cannot outline every cell",
          alternatives=(Candidate("cyto_structural", 0.1),))
print(me.propose(request))
```

```text
[Proposal(predicate=<MarkerPredicate.SEGMENTATION_ROLE: 'segmentation_role'>, value='marker', confidence=0.9, triage=Triage(label=<Attention.SKIM: 'skim'>, reason="confidence 0.90, 0.80 clear of 'cyto_structural' at 0.10"), reasoning='T cells only, so it cannot outline every cell', alternatives=(Candidate(value='cyto_structural', confidence=0.1, reasoning=''),), identity='CD8A', refused=False, notes={})]
```

- `confidence` has no default, because a default would be a number the person never stated.
- `by` has no default and is never read from the environment; this package has no login system. Pass the same name to the store session: `LedgerClaimStore(..., actor=me.by, actor_kind="human")`.

A person gets no exemption from the citation rule.
A rule promoted from an earlier rejection raises rather than quietly rewriting a typed answer:

```python
mito = ProposalRequest(marker="CITRATE_SYNTHASE", evidence="Mitochondrion matrix",
                       locations=("Mitochondrion matrix",))
me.answer(mito, "cyto_structural", confidence=0.6)
```

```text
ValueError: CITRATE_SYNTHASE: segmentation_role='cyto_structural' forbidden - mitochondrial markers are not whole-cell morphology channels
```

```python
me.answer(ProposalRequest(marker="FOXP3", evidence=""), "marker", confidence=0.9)
```

```text
ValueError: FOXP3: nothing is cited, so the claim would not be citable. A human proposal quotes the upstream text it rests on, exactly as a machine one does
```

Both machine proposers demote instead of raising.
Rewriting a generated answer costs nothing; rewriting a person's typed answer would record something they did not say.

## Decide which proposal to review first

Every `Proposal` carries a triage label and its reason.
`review_order` sorts on it: most attention first, then the least sure, then the closest race with the runner-up.
The label is a hint about attention; what the lab decides still goes through a decision.

```python
from clinicalomx_markers.propose.role import RuleBasedRoleProposer
from clinicalomx_markers.propose.schema import ProposalRequest, review_order

requests = [
    ProposalRequest(marker="CITRATE_SYNTHASE", evidence="Mitochondrion matrix",
                    locations=("Mitochondrion matrix",)),
    ProposalRequest(marker="CD8A", evidence="Cell membrane", locations=("Cell membrane",)),
    ProposalRequest(marker="FOXP3", evidence="Nucleus", locations=("Nucleus",)),
]
queue = [(r.marker, RuleBasedRoleProposer().propose(r)[0]) for r in requests]
for marker, p in review_order(queue):
    print(f"{p.triage.label.value:<10} {marker:<16} {p.value:<15} {p.triage.reason}")
```

```text
scrutinise FOXP3            nucleus         confidence 0.45 is below 0.50
review     CD8A             cyto_structural confidence 0.70 against 'marker' at 0.30
skim       CITRATE_SYNTHASE marker          confidence 0.85, 0.80 clear of 'cyto_structural' at 0.05
```

## Next

To change this package, read `docs/maintaining.md`.
