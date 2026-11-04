#!/usr/bin/env python3
"""Fail if a stack's .env lacks a setting its environment.example defines, or still holds a
placeholder.

Usage: env-check.py <stack-directory>     e.g. env-check.py hub-ops

Run by make db-env-check, pipelines-env-check and ops-env-check before each stack starts. A
.env is created once from the example, so a key added later is missing until added by hand.

Examples carry placeholder markers, never real values; a placeholder left in .env is refused.
GRAFANA_ADMIN_PASSWORD guards Grafana's admin surface. Values are printed only from
environment.example, never from .env.
"""

from __future__ import annotations

import sys
from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
# Leading whitespace is allowed, so the indented block report_missing prints can be pasted.
SETTING = re.compile(r"^[ \t]*([A-Z][A-Z0-9_]*)=(.*)$")
PLACEHOLDER = re.compile(r"CHANGEME|VM_FLOATING_IP")

# A key whose example value is not the VM's declares a hint as a comment in its example:
#
#   # env-check-hint LAMIN_DB_HOST_OVERRIDE: ...
#
# report_missing prints it above that key.
HINT = re.compile(r"^#\s*env-check-hint\s+([A-Z][A-Z0-9_]*):\s*(.*)$")

# How a correct .env comes to exist on each stack, printed when one is absent.
ORIGIN = {
    "hub-database": "cp hub-database/environment.example hub-database/.env, then fill it in "
    "(on the VM it is fetched from Barbican by cloud-init)",
    "hub-pipelines": "cp hub-pipelines/environment.example hub-pipelines/.env, then fill it in "
    "(on the VM it is fetched from Barbican by cloud-init)",
    "hub-ops": "`make ops-up` creates it from the example",
}


def settings(path: Path) -> dict[str, str]:
    return dict(
        m.groups() for line in path.read_text().splitlines() if (m := SETTING.match(line))
    )


def hints(path: Path) -> dict[str, str]:
    return dict(
        m.groups() for line in path.read_text().splitlines() if (m := HINT.match(line))
    )


def report_missing(
    example: dict[str, str], actual: dict[str, str], name: str, notes: dict[str, str]
) -> bool:
    missing = sorted(set(example) - set(actual))
    if not missing:
        return False
    print(f"[env] {name} is missing settings that environment.example defines:")
    for key in missing:
        # Every printed line can be appended to a .env; a hint is a comment above its key.
        if key in notes:
            print(f"        # {notes[key]}")
        print(f"        {key}={example[key]}")
    print("[env] Their compose defaults apply instead, which is usually wrong on this host.")
    print(f"[env] Append the lines above to {name}, then re-run.")
    return True


def report_placeholders(example: dict[str, str], actual: dict[str, str], name: str) -> bool:
    unfilled = sorted(
        key
        for key, value in example.items()
        if PLACEHOLDER.search(value) and actual.get(key) == value
    )
    if not unfilled:
        return False
    print(f"[env] {name} still holds the example's placeholder for:")
    for key in unfilled:
        print(f"        {key}={example[key]}")
    print("[env] environment.example says where each real value comes from.")
    print(f"[env] Replace them in {name}, then re-run.")
    return True


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} <stack-directory>")

    stack = Path(sys.argv[1])
    if not stack.is_absolute():
        stack = REPO / stack
    example_path, actual_path = stack / "environment.example", stack / ".env"

    if not example_path.is_file():
        raise SystemExit(f"[env] {example_path} does not exist; is {stack.name} a hub stack?")
    if not actual_path.is_file():
        origin = ORIGIN.get(stack.name, "copy the example beside it and fill it in")
        raise SystemExit(f"[env] {actual_path} does not exist; {origin}")

    name = f"{stack.name}/.env"
    example, actual = settings(example_path), settings(actual_path)
    failed = report_missing(example, actual, name, hints(example_path))
    failed |= report_placeholders(example, actual, name)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
