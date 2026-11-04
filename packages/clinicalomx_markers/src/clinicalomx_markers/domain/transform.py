"""How a mapping claim reads its value out of a sheet: columns, then a chain of named tools.

The claim's value is the reading, as text::

    Sheet1!B1 | before(" (", last=True)                 -> the target
    Sheet1!B1 | between("(", ")", last=True)            -> the clone
    Sheet1!B1 | after("-", last=True)                   -> the barcode
    Sheet1!A1 + Sheet1!C1 | join(" ")                   -> two columns into one field
    Sheet1!E1                                           -> the column as it stands

One field per claim. A value with no ``|`` is the whole cell unchanged.

Deterministic: no model, no network, no clock. An agent may propose a reading, never run one.
`LIBRARY` holds string tools and `join`, the one reducer. When no tool fits, the only step an
agent may write is `pattern(r"...")`, a regular expression; nothing is ever exec'd. A reading
holding a pattern is bespoke, always `Attention.SCRUTINISE`, and never accepted by an agent.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import re._constants as _sre
import re._parser as _sre_parse
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..propose.schema import Attention

# -- the library ---------------------------------------------------------------------------
# Every tool takes the value first and returns a string. A marker not found gives "", never
# the input passed through.


def _at(value: str, marker: str, last: bool) -> int:
    return value.rfind(marker) if last else value.find(marker)


def split(value: str, sep: str, index: int) -> str:
    """The ``index``-th piece of ``value`` split on ``sep``; negative counts from the end. "" if
    there is no such piece."""
    pieces = value.split(sep)
    return pieces[index] if -len(pieces) <= index < len(pieces) else ""


def between(value: str, open: str, close: str, last: bool = False) -> str:
    """What sits between ``open`` and the next ``close`` after it. ``last`` takes the last
    ``open``."""
    start = _at(value, open, last)
    if start < 0:
        return ""
    start += len(open)
    end = value.find(close, start)
    return value[start:end] if end >= 0 else ""


def before(value: str, marker: str, last: bool = False) -> str:
    """Everything before ``marker``: the first one, or the last with ``last``."""
    at = _at(value, marker, last)
    return value[:at] if at >= 0 else ""


def after(value: str, marker: str, last: bool = False) -> str:
    """Everything after ``marker``: the first one, or the last with ``last``."""
    at = _at(value, marker, last)
    return value[at + len(marker):] if at >= 0 else ""


def trim(value: str) -> str:
    return value.strip()


def casefold(value: str) -> str:
    """``value`` in lower case."""
    return value.casefold()


def join(values: tuple[str, ...], sep: str) -> str:
    """Several columns' cells joined with ``sep``, skipping blanks. Only ever the first step."""
    return sep.join(v for v in values if v)


#: The longest pattern a bespoke step may hold.
MAX_PATTERN = 200


def pattern(value: str, regex: str) -> str:
    """The first match of ``regex`` in ``value``: its first group if any, else the whole match;
    "" when nothing matches."""
    found = re.search(regex, value)
    if found is None:
        return ""
    return (found.group(1) or "") if found.re.groups else found.group(0)


@dataclass(frozen=True)
class Tool:
    """One named step: its code, and whether a reviewer must read it as new code."""

    name: str
    run: Callable[..., str]
    #: Written by the proposer, not taken from the library.
    bespoke: bool = False
    #: Takes several columns' cells and returns one. Only ever a first step.
    reduces: bool = False


#: Every tool a reading may name. The library is every entry that is not bespoke.
TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("split", split), Tool("between", between), Tool("before", before),
    Tool("after", after), Tool("trim", trim), Tool("casefold", casefold),
    Tool("join", join, reduces=True),
    Tool("pattern", pattern, bespoke=True),
)}
LIBRARY = tuple(name for name, t in TOOLS.items() if not t.bespoke)

_TYPES = {"str": str, "int": int, "bool": bool}


# -- a step and a reading ------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One tool with its arguments, in declaration order, defaults filled in."""

    tool: str
    args: tuple[str | int | bool, ...] = ()

    @classmethod
    def of(cls, tool: str, *args, **kwargs) -> Step:
        """The step, with its arguments checked against the tool's own signature."""
        if tool not in TOOLS:
            raise ValueError(f"{tool!r} is not a transform tool; the library is "
                             f"{', '.join(LIBRARY)}, and a new one is written as pattern(...)")
        params = list(inspect.signature(TOOLS[tool].run).parameters.values())[1:]
        try:
            bound = inspect.Signature(params).bind(*args, **kwargs)
        except TypeError as wrong:
            raise ValueError(f"{tool}(): {wrong}") from None
        bound.apply_defaults()
        for p in params:
            got, kind = bound.arguments[p.name], _TYPES[str(p.annotation)]
            if type(got) is not kind:
                raise ValueError(f"{tool}(): {p.name} is a {kind.__name__}, got {got!r}")
            # join("") is concatenation; every other blank argument is refused.
            if kind is str and not got and tool != "join":
                raise ValueError(f"{tool}(): {p.name} is blank, so it marks nothing")
        if tool == "pattern":
            _check_pattern(bound.arguments["regex"])
        return cls(tool, tuple(bound.arguments[p.name] for p in params))

    @property
    def bespoke(self) -> bool:
        return TOOLS[self.tool].bespoke

    def text(self) -> str:
        """``between("(", ")", last=True)``: required arguments by position, optional ones by name
        only where they differ from the default."""
        params = list(inspect.signature(TOOLS[self.tool].run).parameters.values())[1:]
        shown = []
        for p, arg in zip(params, self.args, strict=True):
            if p.default is inspect.Parameter.empty:
                shown.append(_literal(arg))
            elif arg != p.default:
                shown.append(f"{p.name}={_literal(arg)}")
        return f"{self.tool}({', '.join(shown)})"


def _literal(arg: str | int | bool) -> str:
    return json.dumps(arg) if isinstance(arg, str) else repr(arg)


def _check_pattern(regex: str) -> None:
    if len(regex) > MAX_PATTERN:
        raise ValueError(f"pattern(): {len(regex)} characters, over the {MAX_PATTERN} a "
                         "reviewer can be asked to read")
    try:
        compiled = re.compile(regex)
    except re.error as wrong:
        raise ValueError(f"pattern(): not a regular expression: {wrong}") from None
    if compiled.groups > 1:
        raise ValueError("pattern(): at most one group, the part the step keeps")
    if _nested_repeat(_sre_parse.parse(regex)):
        raise ValueError("pattern(): a repeated group holds another repeat, such as (\\w+)+, "
                         "which can take minutes on one cell that does not match; repeat "
                         "each part once")


_REPEATS = (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT)


def _nested_repeat(parsed, inside: bool = False) -> bool:
    """Some repeat that can match more than once holds another repeat."""
    for op, av in parsed:
        if op in _REPEATS:
            _, high, body = av
            if inside or _nested_repeat(body, high > 1):
                return True
        elif any(_nested_repeat(sub, inside) for sub in _subpatterns(av)):
            return True
    return False


def _subpatterns(av) -> list:
    if isinstance(av, _sre_parse.SubPattern):
        return [av]
    if isinstance(av, (tuple, list)):
        return [sub for item in av for sub in _subpatterns(item)]
    return []


@dataclass(frozen=True)
class Reading:
    """Which columns a claim reads, and the chain its value goes through.

    ``columns`` are A1 addresses such as ``Sheet1!B1``, never headers. Several columns must start
    with ``join``; one column must not.
    """

    columns: tuple[str, ...]
    steps: tuple[Step, ...] = ()

    def __post_init__(self) -> None:
        if not self.columns or any(not _ADDRESS.fullmatch(c) for c in self.columns):
            raise ValueError(f"a reading names one or more column addresses such as "
                             f"Sheet1!B1, got {list(self.columns)}")
        reducers = [i for i, s in enumerate(self.steps) if TOOLS[s.tool].reduces]
        if len(self.columns) > 1 and reducers != [0]:
            raise ValueError(f"{len(self.columns)} columns are made one value by a first "
                             "join(...) and by nothing else")
        if len(self.columns) == 1 and reducers:
            raise ValueError("join(...) makes several columns one; this reads one column")

    @property
    def bespoke(self) -> bool:
        """Some step was written by the proposer rather than taken from the library."""
        return any(s.bespoke for s in self.steps)

    @property
    def chain(self) -> str:
        """The steps alone as text, with no column: what `usage` counts."""
        return " | ".join(s.text() for s in self.steps) or IDENTITY

    def text(self) -> str:
        """The claim value: ``Sheet1!B1 | after("-", last=True)``. `parse` reads it back."""
        return " | ".join([" + ".join(_address(c) for c in self.columns),
                           *(s.text() for s in self.steps)])

    def attention(self) -> tuple[Attention, str]:
        return attention(self.steps)

    def apply(self, cells: tuple[str, ...]) -> str:
        """One row's cells, in ``columns`` order, as the value this reading gives. Pure."""
        if len(cells) != len(self.columns):
            raise ValueError(f"{len(self.columns)} column(s) read, {len(cells)} cell(s) given")
        if len(self.columns) > 1:
            return run(self.steps[1:], join(cells, *self.steps[0].args))
        return run(self.steps, cells[0])


def run(steps: tuple[Step, ...], value: str) -> str:
    """``value`` through each one-value step in turn."""
    for step in steps:
        value = TOOLS[step.tool].run(value, *step.args)
    return value


def attention(steps: tuple[Step, ...]) -> tuple[Attention, str]:
    """How much attention a reviewer gives: one library tool is a skim, a chain is a review, and
    bespoke code is scrutinised."""
    shown = " | ".join(s.text() for s in steps) or IDENTITY
    written = next((s for s in steps if s.bespoke), None)
    if written is not None:
        return Attention.SCRUTINISE, f"the transform holds newly written code: {written.text()}"
    if len(steps) > 1:
        return Attention.REVIEW, f"a chain of {len(steps)} library tools: {shown}"
    if steps:
        return Attention.SKIM, f"one library tool: {shown}"
    return Attention.SKIM, "the whole cell, unchanged"


def steps(chain: str) -> tuple[Step, ...]:
    """A chain's text, such as ``between("(", ")") | trim()``, as its steps. Blank and
    ``identity()`` are no steps."""
    if chain.strip() in ("", IDENTITY):
        return ()
    return tuple(_step(s.strip()) for s in _pieces(chain))


_ADDRESS = re.compile(r".+![A-Z]+1")
#: One column at the head of a reading: a sheet quoted as Excel quotes one, ``'IO60 + Met'!B1``
#: with ``''`` for a quote inside it, or a plain ``Sheet1!B1``.
_COLUMN = re.compile(r"\s*(?:'((?:[^']|'')+)'|(.+?))!([A-Z]+1)(?=\s+\+\s|\s*\||\s*$)")
_AND = re.compile(r"\s+\+\s+")
#: How a reading with no steps spells its chain: the whole cell, unchanged.
IDENTITY = "identity()"


def parse(text: str) -> Reading:
    """A claim value as the `Reading` it names. Arguments are literals, parsed, never evaluated.

    >>> parse('Sheet1!B1 | between("(", ")", last=True)').chain
    'between("(", ")", last=True)'
    >>> parse("Sheet1!E1").steps
    ()
    """
    columns, at = [], 0
    while True:
        found = _COLUMN.match(text, at)
        if found is None:
            raise ValueError(f"a reading names one or more column addresses such as "
                             f"Sheet1!B1, got {text!r}")
        quoted, plain, cell = found.groups()
        sheet = quoted.replace("''", "'") if quoted is not None else plain.strip()
        columns.append(f"{sheet}!{cell}")
        more = _AND.match(text, found.end())
        if more is None:
            break
        at = more.end()
    rest = text[found.end():].strip()
    steps = _pieces(rest[1:]) if rest else []
    return Reading(tuple(columns), tuple(_step(s.strip()) for s in steps))


def _address(column: str) -> str:
    """``column`` as a reading spells it, quoting the sheet name where needed."""
    sheet, _, cell = column.rpartition("!")
    if "+" in sheet or "|" in sheet or sheet.startswith("'"):
        return "'" + sheet.replace("'", "''") + "'!" + cell
    return column


def _pieces(text: str) -> list[str]:
    """``text`` cut at every ``|`` outside a quoted string."""
    pieces, current, quoted, escaped = [], [], False, False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "|":
            pieces.append("".join(current))
            current = []
            continue
        current.append(ch)
    if quoted:
        raise ValueError(f"an unterminated string in {text!r}")
    return [*pieces, "".join(current)]


def _step(text: str) -> Step:
    try:
        call = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise ValueError(f"{text!r} is not a step such as trim() or after(\"-\")") from None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
        raise ValueError(f"{text!r} is not a step such as trim() or after(\"-\")")
    args = [_constant(a, text) for a in call.args]
    kwargs = {k.arg: _constant(k.value, text) for k in call.keywords if k.arg}
    if len(kwargs) != len(call.keywords):
        raise ValueError(f"{text!r}: a step takes no **arguments")
    return Step.of(call.func.id, *args, **kwargs)


def _constant(node: ast.expr, text: str) -> str | int | bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _constant(node.operand, text)
        if type(inner) is int:
            return -inner
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool):
        return node.value
    raise ValueError(f"{text!r}: every argument is a string, a whole number or True/False")


# -- what a reading does to a sheet --------------------------------------------------------

#: How many input -> output pairs a preview shows.
EXAMPLES = 5


@dataclass(frozen=True)
class Preview:
    """A reading applied to real rows: how many gave a value, and some of them side by side."""

    reading: str
    attention: Attention
    why: str
    #: Rows that gave a non-blank value, out of ``rows``.
    produced: int
    rows: int
    #: ``(input, output)``, rows that gave nothing first.
    examples: tuple[tuple[str, str], ...]

    def to_json(self) -> dict:
        return {"reading": self.reading, "chain": parse(self.reading).chain,
                "attention": self.attention.value, "why": self.why,
                "produced": self.produced, "rows": self.rows,
                "examples": [{"input": i, "output": o} for i, o in self.examples]}


def preview(reading: Reading, rows: Iterable[tuple[str, ...]], *,
            examples: int = EXAMPLES) -> Preview:
    """``reading`` over every row, each row being its cells in ``reading.columns`` order.

    Examples lead with rows that gave nothing. An all-blank row is counted, never shown.
    """
    rows = list(rows)
    out = [reading.apply(r) for r in rows]
    produced = sum(1 for v in out if v)
    pairs = [(" + ".join(r), v) for r, v in zip(rows, out, strict=True) if any(r)]
    failed = [p for p in pairs if not p[1]]
    shown = (failed + [p for p in pairs if p[1]])[:examples]
    label, why = reading.attention()
    return Preview(reading.text(), label, why, produced, len(rows), tuple(shown))


def preview_counted(reading: Reading, counts: Iterable[tuple[tuple[str, ...], int]], *,
                    examples: int = EXAMPLES) -> Preview:
    """`preview` over ``(cells, row count)`` pairs, as a stored column profile keeps them."""
    counts = list(counts)
    whole = preview(reading, (cells for cells, _ in counts), examples=examples)
    produced = sum(n for cells, n in counts if reading.apply(cells))
    return Preview(whole.reading, whole.attention, whole.why, produced,
                   sum(n for _, n in counts), whole.examples)


# -- the library grows from the work -------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """How often one transform has been accepted, and on which sources."""

    chain: str
    accepted: int
    sources: tuple[str, ...]
    bespoke: bool

    @property
    def promotion_candidate(self) -> bool:
        """A bespoke transform accepted on more than one source. Promote it into `TOOLS` by hand."""
        return self.bespoke and len(self.sources) > 1


def usage(accepted: Iterable[tuple[str, str]]) -> list[Usage]:
    """Each transform's acceptance count, from ``(source, claim value)`` of accepted mapping claims.

    Computed on every call, never stored. Values that are not readings are skipped. Ordered by
    count, then chain.
    """
    count: Counter[str] = Counter()
    sources: dict[str, set[str]] = {}
    bespoke: dict[str, bool] = {}
    for source, value in accepted:
        try:
            reading = parse(value)
        except ValueError:
            continue
        count[reading.chain] += 1
        sources.setdefault(reading.chain, set()).add(source)
        bespoke[reading.chain] = reading.bespoke
    return sorted((Usage(c, n, tuple(sorted(sources[c])), bespoke[c])
                   for c, n in count.items()), key=lambda u: (-u.accepted, u.chain))


def reading(columns: Iterable[str], chain: str = "") -> Reading:
    """The `Reading` of ``chain`` over ``columns``; a blank chain reads the cell unchanged."""
    return Reading(tuple(columns), steps(chain))
