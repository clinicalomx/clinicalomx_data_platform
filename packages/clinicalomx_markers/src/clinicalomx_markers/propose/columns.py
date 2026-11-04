"""A vendor sheet to mapping claims: which column fills which reagent field.

A claim is about a column, never a row: at most one claim per column and field, whatever the
row count. Cell cleaning is done by `sources.sheet` and claims nothing.

A `Proposal` asks where a column goes, so its candidates are fields and its predicate is
`DESTINATION`. The claim states "this field comes from this column": subject the sheet,
predicate the field, value the column address such as ``Sheet1!C1``. `as_mapping_claim`
converts one to the other.

A header shared by several columns is placed, with triage at least REVIEW. A column no field
fits is reported as `Unplaced`. One column may fill several fields, as one claim per field,
each with its own `domain.transform` chain. The rule-based proposer tries one library tool,
only for fields with a value test.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path

from ..domain import transform
from ..domain.reagent_mapping import MappingClaim, ReagentMappingPredicate, ReagentMappingSource
from ..sources.sheet import SheetColumn, header_key, profile, read_columns
from .schema import (
    Attention,
    Candidate,
    Proposal,
    ProposalRequest,
    Proposer,
    Reason,
    ReasonKind,
    Triage,
    assess,
    confidence_from,
    ranked,
)

#: A column proposal's ``predicate``. Its value is the field the column fills.
DESTINATION = "destination"


def _dye_key(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.casefold())


@cache
def _known_dyes() -> frozenset[str]:
    """Every canonical dye and curated dye alias, collapsed. Read from the seed file."""
    from clinicalomx_reagents.rows import fluorophores

    names = [r["canonical_name"] for r in fluorophores()]
    names += [a for r in fluorophores() for a in (r.get("aliases") or "").split("|") if a.strip()]
    return frozenset(_dye_key(n) for n in names)


def _regex(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern, re.IGNORECASE)
    return lambda value: compiled.fullmatch(value) is not None


def _is_known_dye(value: str) -> bool:
    return _dye_key(value) in _known_dyes()


@dataclass(frozen=True)
class FieldSignature:
    """What a column filling one reagent field looks like.

    ``names`` are header keys (`sources.sheet.header_key`) that name the field outright;
    ``shared`` could name it among others. ``matches`` tests one cleaned cell and ``says``
    describes the test. A field with no ``matches`` is judged on its header alone.
    """

    predicate: ReagentMappingPredicate
    names: frozenset[str]
    shared: frozenset[str] = frozenset()
    matches: Callable[[str], bool] | None = None
    says: str = ""
    test: ReasonKind = ReasonKind.PATTERN

    def header_score(self, key: str) -> float:
        if key in self.names or key == header_key(self.predicate):
            return 1.0
        return 0.5 if key in self.shared else 0.0

    def tally(self, filled: tuple[str, ...]) -> tuple[int, int] | None:
        """``(matched, filled)``, or None where there is no test or nothing to test."""
        if self.matches is None or not filled:
            return None
        return sum(1 for v in filled if self.matches(v)), len(filled)


_BARCODE = frozenset({"barcode", "barcode id", "oligo", "barcode number"})
_PRODUCT = frozenset({"product id", "product number", "part number", "part no", "id", "sku",
                      "product", "item number"})

#: One signature per reagent field. Spellings are header keys, so "Cat. No." is "cat no".
#: Vocabulary from the seed spec and common vendor spellings. akoya_id and
#: catalogue_number have no value test: no real Akoya id format has been seen yet.
SIGNATURES: tuple[FieldSignature, ...] = (
    FieldSignature(ReagentMappingPredicate.RX_BARCODE,
                   frozenset({"rx", "rx barcode", "reporter barcode", "reporter",
                              "reporter id", "reporter oligo"}),
                   _BARCODE, _regex(r"RX[0-9]+"), "RX[0-9]+"),
    FieldSignature(ReagentMappingPredicate.BX_BARCODE,
                   frozenset({"bx", "bx barcode", "antibody barcode", "antibody oligo"}),
                   _BARCODE, _regex(r"BX[0-9]+"), "BX[0-9]+"),
    FieldSignature(ReagentMappingPredicate.AKOYA_ID,
                   frozenset({"akoya", "akoya id", "akoya product id", "akoya part number"}),
                   _PRODUCT),
    FieldSignature(ReagentMappingPredicate.CATALOGUE_NUMBER,
                   frozenset({"catalogue number", "catalog number", "catalogue no",
                              "catalog no", "cat no", "cat", "catalogue", "catalog"}),
                   _PRODUCT),
    FieldSignature(ReagentMappingPredicate.FLUOROPHORE,
                   frozenset({"fluorophore", "dye", "fluor", "fluorochrome",
                              "reporter dye", "fluorescent dye"}),
                   frozenset({"label", "conjugate", "colour", "color"}),
                   _is_known_dye, "a known dye", ReasonKind.VOCABULARY),
    FieldSignature(ReagentMappingPredicate.TARGET_LABEL,
                   frozenset({"target", "target label", "antibody target", "marker",
                              "analyte", "antigen"}),
                   frozenset({"antibody"})),
    FieldSignature(ReagentMappingPredicate.CLONE,
                   frozenset({"clone", "clone id", "clone name", "antibody clone"}),
                   frozenset({"antibody"})),
)

#: Confidence cap for a column judged on its header alone.
HEADER_ONLY_CEILING = 0.8


@dataclass
class ColumnProposer:
    """Header and value shape to ranked destinations, with no model.

    Every field the evidence points at is a candidate.

    Confidences are an uncalibrated ordinal, as in `role.RuleBasedRoleProposer`.
    """

    name: str = "column-rules"
    signatures: tuple[FieldSignature, ...] = SIGNATURES

    def propose(self, request: ProposalRequest) -> list[Proposal]:
        key = header_key(request.evidence)
        filled = tuple(v for v in request.values if v)
        scored = [self._score(s, key, request.evidence.strip(), filled) for s in self.signatures]
        why = {c.value: reasons for c, reasons in scored}
        candidates = ranked(c for c, _ in scored if c.confidence > 0)
        extracted = [p for sig in self.signatures
                     if (p := self._extraction(sig, key, request, filled)) is not None
                     and p.value != (candidates[0].value if candidates else None)]
        if not candidates and extracted:
            return extracted
        if not candidates:
            reason = (f"header {request.evidence!r} names no reagent field and no value "
                      "matches one")
            return [Proposal(predicate=DESTINATION, value="", confidence=0.0, refused=True,
                             reasoning=reason, triage=Triage(Attention.SCRUTINISE, reason),
                             identity=request.marker)]
        winner, alternatives = candidates[0], candidates[1:]
        return [Proposal(predicate=DESTINATION, value=winner.value,
                         confidence=winner.confidence, reasoning=winner.reasoning,
                         alternatives=alternatives, reasons=why[winner.value],
                         triage=assess(winner.confidence, alternatives),
                         identity=request.marker), *extracted]

    def _extraction(self, sig: FieldSignature, key: str, request: ProposalRequest,
                    filled: tuple[str, ...]) -> Proposal | None:
        """``sig``'s field read out of part of each cell by one library tool, or None.

        The tool must beat the whole cell on the field's value test and reach `EXTRACT_FLOOR`.
        """
        whole = sig.tally(filled)
        if whole is None:
            return None
        best: tuple[float, str, tuple[str, ...]] | None = None
        for chain in candidate_chains(filled):
            steps = transform.steps(chain)
            out = tuple(transform.run(steps, v) for v in filled)
            matched, total = sig.tally(out)
            if matched / total > (best[0] if best else 0.0):
                best = (matched / total, chain, out)
        if best is None or best[0] < EXTRACT_FLOOR or best[0] <= whole[0] / whole[1]:
            return None
        _, chain, out = best
        reasons = reasons_for(sig, key, request.evidence.strip(), out, through=chain)
        confidence = confidence_from(reasons)
        return Proposal(predicate=DESTINATION, value=sig.predicate.value,
                        confidence=confidence, reasons=reasons, transform=chain,
                        reasoning=f"{chain} reads {sig.predicate.value} out of each cell",
                        triage=transformed_triage(confidence, (), chain),
                        identity=request.marker)

    def _score(self, sig: FieldSignature, key: str, header: str,
               filled: tuple[str, ...]) -> tuple[Candidate, tuple[Reason, ...]]:
        reasons = reasons_for(sig, key, header, filled)
        heard = sig.header_score(key)
        said = {1.0: "header names it", 0.5: "header could name it", 0.0: "header silent"}
        tally = sig.tally(filled)
        shape = ("no value test" if tally is None
                 else f"{tally[0]}/{tally[1]} values match {sig.says}")
        return Candidate(sig.predicate.value, confidence_from(reasons),
                         f"{said[heard]}; {shape}"), reasons


#: Tried in this order; a tie goes to the earlier one.
SEPARATORS = (" (", "-", "_", "/", "|", ";", ":", ",", " ")
BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))

#: Share of filled cells a one-tool extraction must make pass the field's test.
EXTRACT_FLOOR = 0.9


def candidate_chains(filled: tuple[str, ...]) -> list[str]:
    """Every one-tool chain worth trying: after the last and before the first of each separator,
    and inside the last of each bracket pair, that the cells hold."""
    chains = []
    for sep in SEPARATORS:
        if any(sep in v for v in filled):
            chains += [transform.Step.of("after", sep, last=True).text(),
                       transform.Step.of("before", sep).text()]
    for open_, close in BRACKETS:
        if any(open_ in v and close in v for v in filled):
            chains.append(transform.Step.of("between", open_, close, last=True).text())
    return chains


def transformed_triage(confidence: float, alternatives: tuple[Candidate, ...],
                       chain: str) -> Triage:
    """`schema.assess`, raised by what reading ``chain`` asks of a reviewer."""
    return raised(assess(confidence, alternatives), chain)


def raised(triage: Triage, chain: str) -> Triage:
    """``triage``, raised by what reading ``chain`` asks of a reviewer, with the transform's reason
    first. Unchanged when the chain asks no more."""
    label, why = transform.attention(transform.steps(chain))
    order = list(Attention)
    if order.index(label) >= order.index(triage.label):
        return triage
    return Triage(label, f"{why}; {triage.reason}")


def reasons_for(sig: FieldSignature, key: str, header: str,
                filled: tuple[str, ...], through: str = "") -> tuple[Reason, ...]:
    """Why a column would fill ``sig``'s field, one reason per check this proposer runs.

    ``through`` names the chain ``filled`` was read through, if any.
    """
    field = sig.predicate.value
    heard = sig.header_score(key)
    semantic = {
        1.0: f"Header {header!r} is one of the vocabulary's own names for {field}.",
        0.5: f"Header {header!r} could name {field}, and names other fields too.",
        0.0: f"Header {header!r} is not a name the vocabulary has for {field}.",
    }[heard]
    reasons = [Reason(ReasonKind.SEMANTIC, semantic, heard)]
    tally = sig.tally(filled)
    if tally is not None:
        matched, total = tally
        read = f" through {through}" if through else ""
        says = (f"{matched}/{total} filled values{read} resolve to {sig.says} in the "
                "vocabulary." if sig.test is ReasonKind.VOCABULARY
                else f"{matched}/{total} filled values{read} match {sig.says}.")
        reasons.append(Reason(sig.test, says, matched / total))
        return tuple(reasons)
    why = (f"No value test exists for {field}, so the values were not checked."
           if sig.matches is None else "The column has no values, so none could be checked.")
    reasons.append(Reason(ReasonKind.NOT_APPLICABLE, why, None, check="value test"))
    if heard > 0:
        reasons.append(Reason(
            ReasonKind.HELD_BACK,
            f"Judged on the header alone, so confidence is capped at "
            f"{HEADER_ONLY_CEILING:.0%} of the header score.",
            -round(heard * (1 - HEADER_ONLY_CEILING), 3)))
    return tuple(reasons)


@dataclass(frozen=True)
class Unplaced:
    """A column that became no claim, and why."""

    column: SheetColumn
    reason: str
    proposal: Proposal | None = None


@dataclass(frozen=True)
class SheetMapping:
    """One sheet's result: a claim per placed column, and every column left over."""

    subject: str
    claims: tuple[MappingClaim, ...]
    #: Every proposal beside its column, placed or not.
    proposals: tuple[tuple[SheetColumn, Proposal], ...]
    unplaced: tuple[Unplaced, ...] = field(default=())
    #: Every column read, in sheet order, placed or not.
    columns: tuple[SheetColumn, ...] = field(default=())
    #: Each claim beside the column it reads and the proposal it came from, in claim order.
    placed: tuple[tuple[SheetColumn, Proposal, MappingClaim], ...] = field(default=())


def request_for(column: SheetColumn) -> ProposalRequest:
    """The `ProposalRequest` every proposer, machine or human, gets for one column."""
    return ProposalRequest(marker=column.address, evidence=column.header, values=column.values)


def evidence_for(column: SheetColumn, predicate: str,
                 signatures: Iterable[FieldSignature] = SIGNATURES, *,
                 chain: str = "") -> str:
    """Evidence text for a column: its header, how many rows the transform read, and how many
    values match the field's test."""
    sig = next(s for s in signatures if s.predicate == predicate)
    steps = transform.steps(chain)
    values = tuple(transform.run(steps, v) for v in column.filled)
    tally = sig.tally(values)
    if sig.matches is None:
        shape = f"no value test for {predicate}"
    elif tally is None:
        shape = "no values"
    else:
        shape = f"{tally[0]}/{tally[1]} values match {sig.says}"
    if not steps:
        return f"header {column.header!r}; {shape}"
    produced = sum(1 for v in values if v)
    return (f"header {column.header!r}; {produced}/{len(column.filled)} filled cells give a "
            f"value through {transform.reading((column.address,), chain).chain}; {shape}")


def as_mapping_claim(proposal: Proposal, column: SheetColumn, *, subject: str,
                     source: str = ReagentMappingSource.SPREADSHEET,
                     source_version: str = "") -> MappingClaim:
    """The column-facing proposal as the field-facing claim, carrying its confidence.

    The value is the column read through the proposal's transform, as `transform.Reading` text.
    """
    predicate = ReagentMappingPredicate(proposal.value)
    value = transform.reading((column.address,), proposal.transform).text()
    return MappingClaim(subject=subject, predicate=predicate, value=value, source=source,
                        source_id=column.address, source_version=source_version,
                        evidence=evidence_for(column, predicate, chain=proposal.transform),
                        confidence=proposal.confidence)


def map_columns(subject: str, columns: Iterable[SheetColumn], *,
                proposer: Proposer | None = None,
                source: str = ReagentMappingSource.SPREADSHEET,
                source_version: str = "") -> SheetMapping:
    """Every usable proposal for a column becomes one claim; every column with none is reported.

    ``proposer`` is any `Proposer`, the rule-based one by default. A `human.HumanProposer`
    answering with ``predicate=DESTINATION`` is the person's route.
    """
    proposer = proposer or ColumnProposer()
    columns = list(columns)
    sharing: dict[str, list[str]] = defaultdict(list)
    for column in columns:
        if column.name:
            sharing[header_key(column.header)].append(column.address)
    claims, proposals, unplaced, placed_on = [], [], [], []
    for column in columns:
        if not column.name:
            unplaced.append(Unplaced(column, f"{column.address} has no header, so a claim "
                                             "would name no column"))
            continue
        found = proposer.propose(request_for(column))
        usable = [p for p in found if p.usable]
        others = [a for a in sharing[header_key(column.header)] if a != column.address]
        if not usable:
            proposal = found[0] if found else None
            if proposal is not None and others:
                proposal = replace(proposal,
                                   triage=_shared_header(proposal.triage, column, others))
            if proposal is not None:
                proposals.append((column, proposal))
            reason = proposal.reasoning if proposal else f"{proposer.name} proposed nothing"
            unplaced.append(Unplaced(column, reason, proposal))
            continue
        placed = False
        for proposal in usable:
            if others:
                proposal = replace(proposal,
                                   triage=_shared_header(proposal.triage, column, others))
            if proposal.transform:
                proposal = _held_back_by_duplicate(proposal, column, columns)
            proposals.append((column, proposal))
            wrong = _not_a_destination(proposal)
            if wrong:
                continue
            claim = as_mapping_claim(proposal, column, subject=subject, source=source,
                                     source_version=source_version)
            claims.append(claim)
            placed_on.append((column, proposal, claim))
            placed = True
        if not placed:
            wrong = _not_a_destination(usable[0])
            unplaced.append(Unplaced(column, f"{column.address}: {wrong}", usable[0]))
    return SheetMapping(subject, tuple(claims), tuple(proposals), tuple(unplaced),
                        tuple(columns), tuple(placed_on))


#: Share of a transformed column's values that must equal another column's, row by row,
#: before the proposal is held back as a duplicate.
DUPLICATE_SHARE = 0.9
#: Confidence change for a duplicate.
DUPLICATE_WEIGHT = -0.25


def _held_back_by_duplicate(proposal: Proposal, column: SheetColumn,
                            columns: list[SheetColumn]) -> Proposal:
    """``proposal`` with lower confidence where its values repeat another column's, row by row."""
    steps = transform.steps(proposal.transform)
    out = [transform.run(steps, v) for v in column.values]
    produced = sum(1 for v in out if v)
    for other in columns:
        if other.address == column.address or not produced:
            continue
        same = sum(1 for v, o in zip(out, other.values, strict=False) if v and v == o)
        if same / produced >= DUPLICATE_SHARE:
            held = Reason(ReasonKind.HELD_BACK,
                          f"{same} of {produced} values equal column {other.address} "
                          f"({other.header!r}) row by row, so that column may already "
                          f"supply {proposal.value}.", DUPLICATE_WEIGHT)
            reasons = (*proposal.reasons, held)
            confidence = confidence_from(reasons)
            triage = transformed_triage(confidence, proposal.alternatives, proposal.transform)
            return replace(proposal, reasons=reasons, confidence=confidence,
                           triage=Triage(triage.label, f"{triage.reason}; duplicates "
                                                       f"{other.address}"))
    return proposal


def _shared_header(triage: Triage, column: SheetColumn, others: list[str]) -> Triage:
    label = Attention.REVIEW if triage.label is Attention.SKIM else triage.label
    return Triage(label, f"header {column.name!r} is shared with {', '.join(others)}; "
                         f"{triage.reason}")


def _not_a_destination(proposal: Proposal) -> str:
    """Why a usable proposal names no reagent field, or ``""`` where it does."""
    if proposal.predicate != DESTINATION:
        return (f"proposal predicate is {proposal.predicate!r}, not {DESTINATION!r}, so it "
                "does not say which field the column fills")
    if proposal.value not in {p.value for p in ReagentMappingPredicate}:
        return f"{proposal.value!r} is not a reagent field"
    return ""


def column_profile(column: SheetColumn, predicate: str | None = None) -> dict:
    """`sources.sheet.profile` of ``column``, noting each listed value that fails the test of the
    field ``predicate`` names. Pass no field for a column read through a transform."""
    sig = next((s for s in SIGNATURES if s.predicate == predicate), None)
    if sig is None or sig.matches is None:
        return profile(column)
    wrong = (f"not {sig.says} in the vocabulary" if sig.test is ReasonKind.VOCABULARY
             else f"does not match {sig.says}")
    return profile(column, note=lambda v: "" if sig.matches(v) else wrong)


def source_profile(mapping: SheetMapping, *, file: str, content_hash: str) -> dict:
    """The sheet a mapping was read from, and every column it left unclaimed with why."""
    columns = mapping.columns
    return {"file": file, "sheet": columns[0].sheet if columns else "",
            "rows": len(columns[0].values) if columns else 0, "columns": len(columns),
            "content_hash": content_hash,
            "unclaimed": [{**column_profile(u.column), "reason": u.reason}
                          for u in mapping.unplaced]}


def read_sheet(path: str | Path, *, subject: str | None = None, sheet: str | None = None,
               proposer: Proposer | None = None,
               source_version: str = "") -> SheetMapping:
    """Read one sheet of a ``.csv`` or ``.xlsx`` into mapping claims.

    For a registered spreadsheet, pass ``artifact.cache()`` as ``path`` and ``artifact.key`` as
    ``subject``. ``subject`` defaults to the file name.
    """
    return map_columns(subject or Path(path).name, read_columns(path, sheet=sheet),
                       proposer=proposer, source_version=source_version)
