"""Constraints promoted from human rejections, stored as data in `seed_data/rules.csv`.

A rule stops the agent proposing a rejected value again.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..seed_data.rows import rules as _rule_rows


@dataclass(frozen=True)
class Rule:
    predicate: str
    when_field: str
    when_contains: str
    forbid_value: str
    reason: str

    def blocks(self, predicate: str, value: str, subject: dict[str, str]) -> bool:
        """True if this rule forbids asserting ``value`` for ``predicate`` on ``subject``."""
        if predicate != self.predicate or value != self.forbid_value:
            return False
        field = (subject.get(self.when_field) or "").lower()
        return self.when_contains.lower() in field


def load_rules() -> list[Rule]:
    return [
        Rule(
            predicate=r["predicate"],
            when_field=r["when_field"],
            when_contains=r["when_contains"],
            forbid_value=r["forbid_value"],
            reason=r["reason"],
        )
        for r in _rule_rows()
    ]


def check(predicate: str, value: str, subject: dict[str, str]) -> None:
    """Raise if any rule forbids this assertion, such as the mitochondrial veto."""
    for rule in load_rules():
        if rule.blocks(predicate, value, subject):
            raise ValueError(
                f"{subject.get('canonical_name', '?')}: {predicate}={value!r} forbidden - "
                f"{rule.reason}"
            )
