"""The site configuration and its consumers name the same values.

site.yaml is read only by scripts/site/render.py, which writes site.env; every consumer reads an
environment variable from it. Nothing at run time notices a consumer naming a variable the
renderer never writes: Prefect registers an unset `{{ $NAME }}` as literal text, and a shell or
compose default quietly stands in. So this holds the two lists together, following
tools/parity/runtime_defs_check.py:

1. site.example.yaml validates against render.py's model, each <...> read as its contents.
2. Every CLINICALOMX_SITE_* name in a tracked file, and every `{{ $NAME }}` in a prefect.yaml, is
   a name render.py emits. That catches a misspelt name wherever it is spelled.
3. Every name render.py emits is read by a tracked file other than the renderer, this check and
   the documentation, so site.env carries nothing dead.
4. site.yaml, site.env, the rendered users.conf and the rendered alert rules are not tracked.

    uv run --no-project --with pydantic --with pyyaml python tools/drift/site_keys.py
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RENDER = "scripts/site/render.py"
THIS = "tools/drift/site_keys.py"
SITE_NAME = re.compile(r"\bCLINICALOMX_SITE_[A-Z0-9_]+")
PREFECT_ENV = re.compile(r"\{\{\s*\$([\w.\-\[\]]+)\s*\}\}")
UNTRACKED = ("site.yaml", "site.env",
             "packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf",
             "packages/clinicalomxdb/hub-ops/grafana/provisioning/alerting/rules.yaml")


def _render():
    """Import `scripts/site/render.py` by path; it is a script, not an installed module."""
    spec = importlib.util.spec_from_file_location("site_render", ROOT / RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strings(value):
    """Every string anywhere in a nested dict or list, depth first."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _tracked_text() -> dict[str, str]:
    """Every tracked TEXT file, path to contents. `git grep -Il ""` is what excludes binaries."""
    files = subprocess.run(["git", "-C", str(ROOT), "grep", "-Il", ""],
                           check=True, capture_output=True, text=True).stdout.split()
    return {f: (ROOT / f).read_text(errors="replace") for f in files if (ROOT / f).is_file()}


def main() -> int:
    """Check the three invariants; print each failure and exit 1 if there was one.

    The example validates, every site name a tracked file reads is one the renderer emits, and
    every emitted name has a reader. A name with no reader is as wrong as a reader with no name:
    it means a consumer was deleted and the key outlived it.
    """
    render = _render()
    problems = []
    try:
        emitted = set(render.environment(render.load(ROOT / "site.example.yaml", example=True)))
    except Exception as exc:  # noqa: BLE001 - any refusal of the example is the finding
        print(f"site.example.yaml does not validate against {RENDER}: {exc}", file=sys.stderr)
        return 1

    text = _tracked_text()
    for path, body in text.items():
        if path in (RENDER, THIS):
            continue
        names = set(SITE_NAME.findall(body))
        if path.endswith("prefect.yaml"):
            # The values, as Prefect templates them; a comment is never substituted.
            names |= {n for value in _strings(yaml.safe_load(body)) for n in PREFECT_ENV.findall(value)}
        for name in sorted(names - emitted):
            problems.append(f"{path}: names {name}, which {RENDER} does not emit")

    consumers = {p: b for p, b in text.items()
                 if p not in (RENDER, THIS, "site.example.yaml") and not p.endswith(".md")}
    for name in sorted(emitted):
        if not any(re.search(rf"\b{name}\b", body) for body in consumers.values()):
            problems.append(f"{RENDER} emits {name}, which no tracked file reads")

    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", *UNTRACKED],
                             check=True, capture_output=True, text=True).stdout.split()
    problems += [f"{path} is tracked; it holds this site's values and must stay ignored"
                 for path in tracked]

    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"site keys: {len(emitted)} names emitted, every one read, every reference emitted")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
