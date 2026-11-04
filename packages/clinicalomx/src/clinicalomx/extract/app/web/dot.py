"""DOT to inline SVG, rendered server-side with one `dot -Tsvg` per draw.

The `dot` binary is optional. Without it, `render` raises `DotUnavailable` and the page shows the
DOT source and the reason.
"""
from __future__ import annotations

import shutil
import subprocess

#: Rendered SVG by DOT source. Bounded.
_CACHE: dict[str, str] = {}
_CACHE_MAX = 64

#: Seconds one render may take.
_TIMEOUT_S = 20


class DotUnavailable(RuntimeError):
    """`dot` could not render this graph. Carries the sentence to put on the page."""


def available() -> bool:
    """Whether a `dot` binary is on PATH at all."""
    return shutil.which("dot") is not None


def render(dot: str) -> str:
    """One DOT graph as an inline `<svg>` element. Raises `DotUnavailable` with the reason."""
    cached = _CACHE.get(dot)
    if cached is not None:
        return cached

    if not available():
        msg = ("Graphviz is not installed here, so the diagram cannot be drawn. The DOT source "
               "is below; `brew install graphviz` or `apt-get install graphviz` renders it.")
        raise DotUnavailable(msg)

    try:
        done = subprocess.run(  # noqa: S603 - argv list, fixed program, input on stdin
            ["dot", "-Tsvg"], input=dot, capture_output=True, text=True,
            timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"Graphviz could not be run: {type(exc).__name__}: {exc}"
        raise DotUnavailable(msg) from exc

    if done.returncode != 0:
        msg = f"Graphviz refused this graph: {done.stderr.strip() or done.returncode}"
        raise DotUnavailable(msg)

    svg = _inline(done.stdout)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[dot] = svg
    return svg


def _inline(svg: str) -> str:
    """The `<svg>` element alone, without fixed width and height, so it scales to its column."""
    start = svg.find("<svg")
    if start == -1:
        return svg
    body = svg[start:]
    head_end = body.find(">")
    head, rest = body[:head_end], body[head_end:]
    for attr in ('width="', 'height="'):
        while (i := head.find(attr)) != -1:
            j = head.find('"', i + len(attr))
            head = head[:i] + head[j + 1:]
    return f'{head} width="100%" style="max-width:100%;height:auto"{rest}'
