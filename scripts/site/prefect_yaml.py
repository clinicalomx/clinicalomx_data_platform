#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Substitute site.env into both prefect.yaml files the way `prefect deploy` does, then check it.

`prefect deploy` replaces each `{{ $NAME }}` with that environment variable and, when the
variable is unset, leaves the placeholder as literal text and registers it (Prefect 3.6.27,
`apply_values(..., remove_notset=False)`). So nothing downstream fails until a run uses the value.

    prefect_yaml.py guard           exit 1 if a placeholder would survive; deploy.sh runs this
    prefect_yaml.py compare REF     what a run receives now, against what it received at REF

`compare` is the parametrisation's acceptance test. For every deployment it compares the pull
steps, the work pool and its job variables, and the EFFECTIVE parameters: what the deployment
passes, plus each flow default it does not override, read from the entrypoint at that revision.
That is the right unit because the storage roots moved from flow defaults into the deployments,
so the text differs while the value a run receives must not. It reads the environment, so render
and source site.env first:

    uv run scripts/site/render.py site.yaml
    set -a; . ./site.env; set +a
    uv run scripts/site/prefect_yaml.py compare platform-v33
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFECT_FILES = (
    "packages/clinicalomx_pipelines/qptiff_pipeline/prefect.yaml",
    "packages/clinicalomx_ml/roi_detection/prefect.yaml",
)
ENV_PLACEHOLDER = re.compile(r"\{\{\s*\$([\w.\-\[\]]+)\s*\}\}")


def substitute(value, env: dict[str, str], missing: set[str]):
    """Prefect's env substitution over parsed YAML, so a comment is never templated."""
    if isinstance(value, dict):
        return {k: substitute(v, env, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, env, missing) for v in value]
    if not isinstance(value, str):
        return value

    def one(match: re.Match) -> str:
        name = match.group(1)
        if not env.get(name):
            missing.add(name)
            return match.group(0)
        return env[name]

    return ENV_PLACEHOLDER.sub(one, value)


def rendered(rel: str) -> tuple[dict, set[str]]:
    import yaml

    missing: set[str] = set()
    doc = substitute(yaml.safe_load((ROOT / rel).read_text()), dict(os.environ), missing)
    return doc, missing


def guard() -> int:
    failed = False
    for rel in PREFECT_FILES:
        _, missing = rendered(rel)
        for name in sorted(missing):
            print(f"REFUSED {rel}: {{{{ ${name} }}}} is unset or empty, and prefect deploy would "
                  "register it as literal text", file=sys.stderr)
            failed = True
    if not failed:
        print("every site placeholder in both prefect.yaml files is set")
    return 1 if failed else 0


def _git_show(ref: str, rel: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{rel}"],
                          check=True, capture_output=True, text=True).stdout


def _flow_defaults(source: str, function: str) -> dict:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            args = node.args.posonlyargs + node.args.args
            pairs = list(zip(args[len(args) - len(node.args.defaults):], node.args.defaults))
            pairs += [(a, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d]
            out = {}
            for arg, default in pairs:
                try:
                    out[arg.arg] = ast.literal_eval(default)
                except ValueError:
                    out[arg.arg] = ast.unparse(default)
            return out
    raise LookupError(f"no function {function!r}")


def _effective(doc: dict, rel: str, read) -> dict[str, dict]:
    """Each deployment, with `parameters` replaced by what a run of it actually receives."""
    project = str(Path(rel).parent)
    out = {}
    for dep in doc["deployments"]:
        path, function = dep["entrypoint"].split(":")
        dep = dict(dep)
        dep["parameters"] = {**_flow_defaults(read(f"{project}/{path}"), function),
                             **(dep.get("parameters") or {})}
        out[dep["name"]] = dep
    return out


def _diff(a, b, where: str) -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            if key not in a:
                out.append(f"{where}.{key}: only now: {b[key]!r}")
            elif key not in b:
                out.append(f"{where}.{key}: only at the ref: {a[key]!r}")
            else:
                out += _diff(a[key], b[key], f"{where}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _diff(x, y, f"{where}[{i}]")]
    return [] if a == b else [f"{where}: {a!r} at the ref, {b!r} now"]


def compare(ref: str) -> int:
    import yaml

    problems = []
    for rel in PREFECT_FILES:
        now, missing = rendered(rel)
        if missing:
            problems.append(f"{rel}: unset {sorted(missing)}; render and source site.env first")
            continue
        then = yaml.safe_load(_git_show(ref, rel))
        found = _diff({k: v for k, v in then.items() if k != "deployments"},
                      {k: v for k, v in now.items() if k != "deployments"}, rel)
        found += _diff(_effective(then, rel, lambda p: _git_show(ref, p)),
                       _effective(now, rel, lambda p: (ROOT / p).read_text()), rel)
        problems += found
        print(f"{rel}: {len(now['deployments'])} deployments, "
              f"{'IDENTICAL to ' + ref if not found else f'{len(found)} difference(s)'}")
    for problem in problems:
        print(f"DIFFERS {problem}", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["guard"] and len(argv) == 1:
        return guard()
    if argv[:1] == ["compare"] and len(argv) == 2:
        return compare(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
