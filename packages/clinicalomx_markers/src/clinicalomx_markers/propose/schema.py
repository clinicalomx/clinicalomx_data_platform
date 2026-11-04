"""Contracts between a proposer and the claim lifecycle.

A `Proposal` carries the winner with its confidence, ranked runners-up, and a triage label
with its reason. The triage label is `Attention`, a plain `Enum` that can never be used as a
review status.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Protocol


@dataclass(frozen=True)
class ProposalRequest:
    """Everything a proposer may look at. No live network: the snapshot is already fetched."""

    marker: str
    #: Verbatim upstream text, as a citation quotes it.
    evidence: str
    #: Normalised localisation terms.
    locations: tuple[str, ...] = ()
    go_cc: tuple[str, ...] = ()
    #: Steering the human added on earlier attempts, oldest first.
    steering: tuple[str, ...] = ()
    attempt: int = 1
    #: A sheet column's cleaned cells, for a column proposer. ``marker`` is then the column's name
    #: and ``evidence`` its header.
    values: tuple[str, ...] = ()


def _check_confidence(value: float, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{what}: confidence must be a number in [0, 1], got {value!r}")


@dataclass(frozen=True)
class Candidate:
    """One value a proposer considered, with how strongly it holds it."""

    value: str
    #: 0 to 1, as the proposer states it.
    confidence: float
    reasoning: str = ""

    def __post_init__(self) -> None:
        _check_confidence(self.confidence, f"candidate {self.value!r}")


class Attention(Enum):
    """How much of a reviewer's attention a proposal needs, most first. Never a decision."""

    SCRUTINISE = "scrutinise"
    REVIEW = "review"
    SKIM = "skim"


@dataclass(frozen=True)
class Triage:
    """A machine attention label and the reason for it."""

    label: Attention
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.label, Attention):
            raise TypeError(f"a triage label is an Attention, got {self.label!r}")
        if not self.reason.strip():
            raise ValueError(f"triage {self.label.value!r} carries no reason")


class ReasonKind(StrEnum):
    """What kind of basis a proposer had for one part of its confidence. Values are display text."""

    #: The header or name is the vocabulary's own term for the slot.
    SEMANTIC = "semantic match"
    #: The values look like what the slot holds, by a test other than a declared pattern.
    SHAPE = "shape match"
    #: The values match a declared pattern, with a count.
    PATTERN = "pattern match"
    #: The values resolve against a controlled vocabulary, with a count.
    VOCABULARY = "vocabulary hit"
    #: An earlier source had this accepted.
    PRECEDENT = "precedent"
    #: What lowered the confidence. Always negative.
    HELD_BACK = "held back"
    #: A check that does not apply here.
    NOT_APPLICABLE = "not applicable"


@dataclass(frozen=True)
class Reason:
    """One named, weighted basis for a proposal, with the sentence of evidence behind it.

    ``weight``:

    - above zero: checked and passed, to that degree;
    - zero: checked and failed;
    - None: not checked (only `ReasonKind.NOT_APPLICABLE`);
    - below zero: held the proposer back (only `ReasonKind.HELD_BACK`).
    """

    kind: ReasonKind
    #: One sentence a reviewer can check, such as "28/28 values match RX[0-9]+".
    evidence: str
    weight: float | None
    #: For a not-applicable reason, the check that could not run, such as "value test".
    check: str = ""

    def __post_init__(self) -> None:
        kind = ReasonKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if not self.evidence.strip():
            raise ValueError(f"a {kind} reason carries no evidence")
        if kind is ReasonKind.NOT_APPLICABLE:
            if self.weight is not None:
                raise ValueError("a not-applicable reason has no weight: nothing was checked")
            if not self.check.strip():
                raise ValueError("a not-applicable reason names the check that did not apply")
            return
        if isinstance(self.weight, bool) or not isinstance(self.weight, int | float):
            raise ValueError(f"a {kind} reason is weighted, got {self.weight!r}")
        if kind is ReasonKind.HELD_BACK:
            if not -1.0 <= self.weight < 0.0:
                raise ValueError(f"a held-back reason lowers confidence, got {self.weight!r}")
        elif not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"a {kind} reason's weight is in [0, 1], got {self.weight!r}")

    @property
    def outcome(self) -> str:
        """``passed``, ``failed``, ``not applicable`` or ``held back``, from the weight."""
        if self.weight is None:
            return "not applicable"
        if self.weight < 0:
            return "held back"
        return "passed" if self.weight > 0 else "failed"

    def to_json(self) -> dict:
        return {"kind": self.kind.value, "weight": self.weight, "evidence": self.evidence,
                "check": self.check, "outcome": self.outcome}

    @classmethod
    def from_json(cls, raw: dict) -> Reason:
        return cls(kind=ReasonKind(raw["kind"]), evidence=raw["evidence"],
                   weight=raw.get("weight"), check=raw.get("check") or "")


def confidence_from(reasons: Iterable[Reason]) -> float:
    """The mean of the checked reasons' weights, plus every held-back weight, clamped to [0, 1]
    and rounded to 3 places. Not-applicable reasons are ignored."""
    reasons = tuple(reasons)
    checked = [r.weight for r in reasons
               if r.weight is not None and r.kind is not ReasonKind.HELD_BACK]
    held = sum(r.weight for r in reasons if r.kind is ReasonKind.HELD_BACK)
    base = sum(checked) / len(checked) if checked else 0.0
    return round(min(1.0, max(0.0, base + held)), 3)


#: At or above this, with the runner-up at least `SKIM_MARGIN` behind, a proposal is skimmed.
SKIM_CONFIDENCE = 0.8
SKIM_MARGIN = 0.3
#: Below this a proposal is scrutinised however far ahead of its runner-up it is.
SCRUTINISE_BELOW = 0.5


def assess(confidence: float, alternatives: tuple[Candidate, ...] = ()) -> Triage:
    """The default triage for a winner and its ranked runners-up. A proposer may override it."""
    if confidence < SCRUTINISE_BELOW:
        return Triage(Attention.SCRUTINISE,
                      f"confidence {confidence:.2f} is below {SCRUTINISE_BELOW:.2f}")
    runner = alternatives[0] if alternatives else None
    margin = confidence - (runner.confidence if runner else 0.0)
    against = (f"{str(runner.value)!r} at {runner.confidence:.2f}" if runner
               else "no alternative stated")
    if confidence >= SKIM_CONFIDENCE and margin >= SKIM_MARGIN:
        return Triage(Attention.SKIM,
                      f"confidence {confidence:.2f}, {margin:.2f} clear of {against}")
    return Triage(Attention.REVIEW, f"confidence {confidence:.2f} against {against}")


@dataclass(frozen=True)
class Proposal:
    """One proposed assertion: the winner, its ranked runners-up, and where to look.

    ``alternatives`` are best first. One may out-rank the winner when a promoted rule demoted the
    answer; ``margin`` is then negative. A refusal has confidence 0.
    """

    predicate: str
    value: str
    confidence: float
    triage: Triage
    #: Short justification for a human. Never evidence for a gate.
    reasoning: str = ""
    alternatives: tuple[Candidate, ...] = ()
    #: What the proposer thinks it identified.
    identity: str = ""
    refused: bool = False
    notes: dict = field(default_factory=dict)
    #: The named, weighted bases for ``confidence`` (`confidence_from`). Empty when the proposer
    #: cannot decompose its number.
    reasons: tuple[Reason, ...] = ()
    #: For a sheet column, the `domain.transform.Reading.chain` text such as
    #: ``after("-", last=True)``. Blank reads the whole cell.
    transform: str = ""

    def __post_init__(self) -> None:
        _check_confidence(self.confidence, f"proposal {self.value!r}")
        if not isinstance(self.triage, Triage):
            raise TypeError(f"a proposal's triage is a Triage, got {self.triage!r}")
        if self.value and self.value in {a.value for a in self.alternatives}:
            raise ValueError(f"{self.value!r} is both the winner and an alternative")
        if self.alternatives != ranked(self.alternatives):
            raise ValueError("alternatives are ranked best first: "
                             f"{[(a.value, a.confidence) for a in self.alternatives]}")

    @property
    def usable(self) -> bool:
        return not self.refused and bool(self.value)

    @property
    def margin(self) -> float:
        """How far the winner leads the runner-up, negative if it trails."""
        return self.confidence - (self.alternatives[0].confidence if self.alternatives else 0.0)


def ranked(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """``candidates`` best first. Stable for equal confidences."""
    return tuple(sorted(candidates, key=lambda c: -c.confidence))


_ATTENTION_RANK = {label: rank for rank, label in enumerate(Attention)}


def review_order(queue: Iterable[tuple[str, Proposal]]) -> list[tuple[str, Proposal]]:
    """``(subject, proposal)`` pairs in review order.

    Scrutinise, then review, then skim. Within a label: least confident, then closest race, then
    subject.
    """
    return sorted(queue, key=lambda item: (_ATTENTION_RANK[item[1].triage.label],
                                           item[1].confidence, item[1].margin, item[0]))


class Proposer(Protocol):
    """Anything that can propose."""

    name: str

    def propose(self, request: ProposalRequest) -> list[Proposal]: ...
