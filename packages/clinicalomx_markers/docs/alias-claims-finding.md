# Aliases as claims: multi-valued predicates

**Decision:** a domain declares `ClaimDomain.multi_valued_predicates`.
On those predicates the value joins the `Assertion` key, so UniProt listing CDHE and UVO for one accession is two assertions.
`MARKER_DOMAIN` declares `alias` alone.
Alias seeding is UniProt only, and `AliasRegistry` is retired.
`tests/test_alias_claims.py` holds the proofs.

The rest of this page is why.

## The problem

The goal: an agent proposes an alias with a confidence, a human can reject it, aliases are seeded from UniProt synonyms, and the claim's subject replaces `AliasRegistry`.

The shared grammar, `clinicalomx_ledger.grammar`, kept one value per `Assertion`, which is (subject, predicate, source, source_id).
`held`, the queue (`services.queue`) and the repeat check (`stored_duplicate`, `services.create_claim`) all keep the newest claim per `Assertion`.

Measured with `MemoryClaimStore` and two UniProt gene synonyms of P12830 on E-CADHERIN:

```text
held     ['UVO']                         CDHE is superseded, and leaves the queue
history  ['CDHE', 'UVO']
re-seed  ['CDHE', 'UVO', 'CDHE', 'UVO']  every re-seed appends both again
```

So two aliases for one subject could not coexist whenever both came from one accession, which is the ordinary UniProt case.
The answer side could already hold several accepted claims per predicate; the source side could not.

## Options weighed

| Option | Pros | Cons |
|---|---|---|
| A. Alias set per source, like `TargetSet` | No grammar or ledger change | Confidence inside the value string (`CDHE=0.90\|UVO=0.60`), one field meaning two things; a retraction is a human claim restating the set, and a later agent re-proposal re-adds it |
| B. Multi-valued predicates in the grammar | One claim per alias; a retraction is an ordinary rejected decision | Changes the shared grammar and both stores |
| C. Encode the synonym into `source_id` | No grammar change | Every reader would have to parse `source_id` |

- **A was refused** for putting confidence inside the value. `Claim.confidence` is now its own field.
- **C was refused** because every reader would have to parse it.
- **B was chosen**, scoped to the predicates a domain declares, so every other predicate keeps one position per source.

## Limits

- Nothing in this repository captures EFO synonyms. EFO is read only as `PLATFORM_TERMS`, and an EFO synonym names a platform, which is not a subject type `MARKER_DOMAIN` permits.
- The code's name for the old registry is `seed_spec.AliasRegistry`.
