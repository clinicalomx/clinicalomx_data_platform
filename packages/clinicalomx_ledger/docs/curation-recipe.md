# Onboarding a curated domain

The contract a new curated domain follows to get claims, a human review gate, and an append-only record of every ruling.
A curated domain is one kind of lab knowledge with its own claims, such as markers.

- Each domain is its own Python distribution. This package is the only thing domains share; there is no umbrella curation package, because it would put two domains' policy in one place.
- `clinicalomx_markers` is the reference consumer and declares two domains this way.
- `clinicalomx_ledger.grammar.example` is the smallest worked example. `tests/test_curation_recipe.py` runs it end to end, so neither this file nor the example can rot silently.

## Steps

1. Declare your vocabularies and models (section 1).
2. Bind them in one `ClaimDomain` (section 1).
3. Declare your lamindb names through a `clinicalomx.vocabulary` entry point (section 1).
4. Prototype against `MemoryClaimStore`, then ship against `LedgerClaimStore` (section 2).
5. Give every agentic proposer a human equivalent (section 3).
6. Check you do none of the things in section 4.

## 1. What your domain declares

Everything the shared machinery cannot guess travels as one frozen `ClaimDomain`.
Only `policy`, `multi_valued_predicates` and `agent_may_accept` have defaults, so a missing field is refused where you build it, not guessed later.

| Field | What it is |
|---|---|
| `name` | The tag every claim and void carries, and part of the resolution key. Append-only forever, so you can never rename it. |
| `claim`, `decision`, `invalidation` | Your three model subclasses, parameterised on your own vocabularies. |
| `subject_types` | What a claim may be ABOUT, as `ln.Record` type names. |
| `reviewed_predicates` | The predicates a human reviews. Only a claim on one of these moves its subject through the queue. |
| `multi_valued_predicates` | Predicates a source may hold several values of at once, such as an alias. The value joins the supersession key, so two values from one source stand side by side. Defaults to none. |
| `claim_type`, `verdict_type`, `invalidation_type` | `ln.Record` type names, declared in your vocabulary. Retired with the old EAV store, and still required. |
| `state_feature` | The `ln.Feature` your subjects' review state is held under. Yours alone: two domains are two queues. |
| `policy` | Your extra constraints on what may be asserted, raising to refuse. Defaults to none, which is right until you have rejected something. |
| `agent_may_accept` | What an agent may not accept even under an approved policy, raising to refuse. A human may still accept it. The store calls it before any write. Defaults to refusing nothing. |

### The two vocabularies

Subclass `ClaimSource` and `ClaimPredicate`.
They are `StrEnum` bases with no members, on purpose: an enum with members cannot be extended.

A predicate string is part of `Claim.claim_id`, so renaming one orphans every citation already written under it.
Pick a spelling you can live with.

### The three models

Subclass the PARAMETERISED generic, never a bare generic, and never re-declare a field:

```python
class SampleClaim(Claim[SampleSource, SamplePredicate], entity="Sample"): ...
class SampleDecision(Decision[SamplePredicate], entity="Sample"): ...
class SampleSourceInvalidation(SourceInvalidation[SampleSource], entity="Sample"): ...
```

- `entity` fills in the field descriptions, which become the JSON schema an LLM proposer reads.
- Parameterising makes your models refuse another domain's vocabulary. That matters most for invalidation: domains commonly all spell `human`, so an unparameterised void would reach claims nobody meant to touch.

### Evidence

- `evidence` is verbatim upstream text, so a citation can quote rather than paraphrase.
- `source_id` is what a citation resolves to upstream: an accession, a PMID, a URL, or a person.
- `source_version` is the upstream release the value was read at. Blank is normal: a human assertion has no release. Release strings cannot be ordered, so a void bounded to a version matches that version exactly.
- A claim is refused as uncitable if `subject`, `predicate`, `value`, `source`, `source_id` or `evidence` is blank or whitespace.
- `confidence` is the proposer's own, from 0 to 1, stored so a reviewer can sort the queue. It is not part of the claim id or of what makes a repeat: saying the same thing more or less surely is not a new assertion.

### Actor typing

Every row names who wrote it.
A database CHECK constraint enforces the shape, so a direct SQL writer is bound too.

- A human session passes `actor_kind="human"` and no model fields.
- An agent session passes `actor_kind="agent"`, plus `agent_model` and `agent_model_version` (both required), and `policy_version`, the policy it proposes under. Every claim it writes records that policy.
- A decision is written as the DECIDING HUMAN, whoever ran the session, with their stated `decided_at`. A claim stays under the session's own identity.
- An agent decision is possible only under a `Policy` a human approved, and a foreign key checks it. Nothing in `grammar.store` writes one: the operator approves it (`docs/runbook.md`).

`decided_at` is when the human says they decided, not when it was recorded.
It is part of the repeat check, so the same human re-affirming on a later date is a second decision.

### Vocabulary declaration

Your `ln.Record` types and your `state_feature` are lamindb names.
Declare them through `clinicalomx.vocab.Vocabulary`, advertised by a `clinicalomx.vocabulary` entry point in your `pyproject.toml`.
Declare each from the module that owns the name, not from a literal, so a rename reaches the declaration automatically.
A consumer owns every name it uses beyond the `clinicalomx` foundation.

## 2. Wiring the gate

```text
claim written -> unresolved -> accepted
                     |
                     +------> rejected   (kind: rule | hint | one_off)
                                 retry: a new claim, which is unresolved again
```

- A claim nobody has ruled on is unresolved because it has no decision; nothing is written to say so.
- The answer is every claim whose newest decision accepts it. The queue is the claims with no decision.

Two methods:

```python
store.add(claim, outcome=..., judgement=..., subject_row=...)   # assert, and optionally rule
store.decide(claim, outcome=..., judgement=...)                 # rule on a claim already written
```

- **Proposals in.** A machine proposal calls `add(claim)` and nothing else. `outcome` and `judgement` stay None, and no decision is written.
- **Judgements out.** A human hands in a `Judgement(by=..., at=..., kind=..., reason=...)` with an `outcome` of accepted or rejected. A blank `by` or `at` is refused: a decision nobody answers for is the defect this ledger removes.
- **Decisions appended.** The store turns the outcome and judgement into a `Decision` on that one claim. It writes one only if the claim's newest decision does not already say the same, so a repeat is a no-op and a second curator's sign-off is a re-affirmation, not a duplicate.
- **Corrections append.** A rejection is a later decision on the claim it rejects, and takes it out of the answer. A correction is a new claim plus a decision. `decide` rules on an earlier claim without the source appearing to say it again. Several claims can answer one predicate at once (an alias); declare the predicate in `multi_valued_predicates` so one source can propose several.
- **The subject's state follows.** It moves to whatever its claims and decisions leave it in, on a repeat as well as a write. `store.in_state("unresolved")` is the queue, an indexed lamindb filter, not a scan.

Two repeat rules, one per table: `stored_duplicate` decides whether the assertion is new, and `decisions_for` whether the ruling is.
Either can be a no-op while the other writes.

Both stores take one `ClaimDomain` and answer identically, because every rule is decided offline and the backend only finds the rows.

`invalidate` is a third writer, and only a human may use it.
An agent that can void its own evidence in bulk can quietly rewrite history, so nothing on your curation path may reach it.

## 3. Every proposing stage takes a human

Wherever an agent can propose, a person must be able to propose the same thing, through an equivalent API, in the same package, at the same stage.
This is a requirement: an agent nobody can stand in for is a single point of failure, and a lab that cannot answer a question by hand cannot audit a model's answer.

- **Same input.** The person gets what the agent gets, and nothing extra.
- **Same output.** One proposal type, so a driver cannot tell which it holds.
- **Same evidence rule.** An unevidenced human assertion is refused exactly as an agent's is. Citability is `validate`'s rule and it does not know about actors.
- **Same refusals.** What your `policy` forbids, it forbids on both paths. How each path REPORTS a refusal is its own: tell a person rather than rewriting their typed answer.

The only difference is actor typing, which lives on the store session, never on a proposal.
The person's identity is an explicit argument; a proposing module must never read it from the environment.

Nothing downstream may branch on which path produced a proposal.
If your gate, policy or write step needs to tell them apart, treat that as a design problem: a rule that holds only for machine proposals is one the ledger cannot audit.

The reference implementation is `clinicalomx_markers.propose`: `human.py` beside `role.py` and `llm.py`, one `Proposer` protocol over all three.

## 4. What your domain must not do

- **No direct ORM writes.** `clinicalomx_ledger.models` is internal. Write through `grammar.store`, or through `clinicalomx_ledger.adapter` if you implement your own store.
- **No second write path.** One store per domain. The refusals (`check_claim_domain`, `validate`, `check_subject_type`, `decisions_for`) all run before any row is written, so a refusal writes nothing. Going around them gains nothing: the schema refuses bad rows anyway, later and less clearly.
- **Read through the read surface:** `clinicalomx_ledger.reads`, or your own store's `held`, `decisions`, `answers` and `invalidations`. Never query the tables.
- **No `status` field on a claim.** An observation is made, not accepted. What was DECIDED belongs to the decision.
- **No `supersedes` or `is_current` field.** `held` is computed from the log, and either field would duplicate what the ordering already says.
- **Never edit to correct.** A correction is a later claim or a later decision. A faulty source with no replacement value is a `SourceInvalidation`. An acceptance that should never have stood is a rejecting decision on the same claim.
- **Never reuse another domain's `state_feature` or `name`.**

## 5. The worked example

`clinicalomx_ledger/grammar/example.py` is the smallest complete declaration that runs: one subject type, one reviewed predicate, two sources, no policy.
It is about sixty lines, and every one is required.

`tests/test_curation_recipe.py` runs it through both stores and asserts what a listing cannot show:

- the domain declares nothing this file does not list;
- the two stores agree;
- a rejection takes the answer away;
- a decision is typed as the deciding human;
- a void deletes nothing.

The instance setup is the same every time, and it is in that test's fixture: one `ln.Record` type per entry in `subject_types`, the subject records themselves, and a branch to curate on.
The state feature creates itself on first use.

## 6. Where things live

| You want | It is in |
|---|---|
| The models and the read rules | `clinicalomx_ledger.grammar.model` |
| The gate | `clinicalomx_ledger.grammar.gate` |
| The stores | `clinicalomx_ledger.grammar.store` |
| The write surface for a custom store | `clinicalomx_ledger.adapter` |
| The reads for a consumer with no store | `clinicalomx_ledger.reads` |
| A real domain to copy | `clinicalomx_markers/src/clinicalomx_markers/domain/marker.py` |
| Operator procedures | `docs/runbook.md` |

Deployment, roles and installing the instance module are the operator's job, not yours.
