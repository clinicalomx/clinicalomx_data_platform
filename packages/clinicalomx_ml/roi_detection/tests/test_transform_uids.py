"""Every lamindb decorator in a module carries that module's pinned Transform uid, or none does.

lamindb keys a Transform on the file. Without a pinned 12-character stem uid, a source change
mints a new Transform instead of a version. Persisted identities cannot be withdrawn, so the
pins are exact. Reads source with ``ast``.
"""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src/croi"

#: The modules whose Transforms have run on the instance, and the stem each continues.
PINNED = {
    "objectives/tma_core/split.py": "uvjWNYOTOsev",
    "train.py": "wl8ndVgVLIMs",
    "evaluation/evaluate.py": "3DdrP0t3bfCL",
}

#: `lamindb.base.uids.base62`'s alphabet, twelve characters long.
STEM = re.compile(r"[0-9A-Za-z]{12}")


def decorator_uids(path: pathlib.Path) -> dict[str, str | None]:
    """``{function name -> uid or None}`` for every ``@ln.step(...)`` and ``@ln.flow(...)``."""
    found = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"step", "flow"}
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "ln"
            ):
                continue
            uid = {k.arg: k.value for k in dec.keywords}.get("uid", dec.args[0] if dec.args else None)
            found[node.name] = None if uid is None else ast.literal_eval(uid)
    return found


def all_modules() -> dict[str, dict[str, str | None]]:
    return {
        str(p.relative_to(SRC)): uids
        for p in sorted(SRC.rglob("*.py"))
        if (uids := decorator_uids(p))
    }


def test_the_transforms_that_have_run_keep_their_stem():
    modules = all_modules()
    for module, stem in PINNED.items():
        assert modules.get(module), f"{module} holds no @ln.step or @ln.flow"
        assert modules[module] == dict.fromkeys(modules[module], stem), module


def test_every_decorator_in_a_module_names_one_transform():
    split = {m: uids for m, uids in all_modules().items() if len(set(uids.values())) > 1}
    assert not split, f"one module, several uids: {split}"


def test_a_pinned_uid_is_a_twelve_character_stem():
    bad = {
        f"{m}:{fn}": uid
        for m, uids in all_modules().items()
        for fn, uid in uids.items()
        if uid is not None and not STEM.fullmatch(uid)
    }
    assert not bad, f"not a 12-character base62 stem: {bad}"
