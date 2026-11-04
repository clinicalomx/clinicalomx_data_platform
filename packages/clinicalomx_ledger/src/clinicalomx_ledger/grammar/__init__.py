"""The claim/decision grammar: the ledger's client-facing Python API.

    model.py    Claim, Decision, SourceInvalidation, ClaimDomain, and the read rules
    gate.py     the review rules: Judgement, decisions_for, subject_state_for
    store.py    the ClaimStore protocol and its two backends
    lineage.py  what the run that produced a claim says
    example.py  the smallest complete domain declaration

Nothing is re-exported. Only `store.py`'s ledger backend needs Django and lamindb.
See `docs/curation-recipe.md` to declare a domain.
"""
