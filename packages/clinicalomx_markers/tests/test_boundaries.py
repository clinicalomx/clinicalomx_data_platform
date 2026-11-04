"""The proposer must not be reachable from the seeder: no model runs at apply time."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Modules that would make a model or network call reachable from the seeder.
_AGENT_ONLY = ("anthropic", "httpx", "langgraph", "openai")

#: Modules that would make the constants unreadable without a database.
_NEEDS_A_DATABASE = ("lamindb", "bionty", "django", "psycopg2")


def _modules(package: str) -> list[str]:
    """Every module under `package`, found on disk. The packages re-export nothing."""
    import clinicalomx_markers

    root = Path(clinicalomx_markers.__file__).parent
    here = root.joinpath(*package.split(".")[1:])
    found = [
        f"{package}.{p.stem}" for p in sorted(here.glob("*.py")) if p.stem != "__init__"
    ]
    assert found, f"no modules under {package}"
    return found


def _in_subprocess(code: str) -> str:
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


#: The packages the vocabulary chain runs through. Each is checked.
_CHAIN_PACKAGES = ("clinicalomx_markers", "clinicalomx_ledger", "clinicalomx_reagents")


def _chain_of(module: str) -> tuple[list[str], list[str]]:
    """What importing `module` loads in a fresh interpreter: its chain in these packages, and
    the forbidden modules it reached."""
    forbidden = _NEEDS_A_DATABASE + _AGENT_ONLY
    out = _in_subprocess(
        f"import {module}, sys;"
        "print(','.join(sorted(m for m in sys.modules"
        f" if m.startswith({_CHAIN_PACKAGES!r}))));"
        f"print(','.join(m for m in {forbidden!r} if m in sys.modules))"
    )
    lines = (out.split("\n") + ["", ""])[:2]
    return [m for m in lines[0].split(",") if m], [m for m in lines[1].split(",") if m]


#: The modules `vocab.py` must reach: each owns names the aggregate declares.
_OWNERS = (
    "clinicalomx_ledger.grammar.model",
    "clinicalomx_ledger.grammar.gate",
    "clinicalomx_markers.domain.marker",
    "clinicalomx_markers.domain.reagent_mapping",
    "clinicalomx_markers.sources.snapshot",
    "clinicalomx_markers.seed_spec",
    "clinicalomx_reagents.spec",
    "clinicalomx_reagents.inventory",
    "clinicalomx_markers.domain.imaging_channel",
)


def test_the_vocabulary_chain_pulls_in_no_heavy_dependency():
    """The constants are readable without lamindb, bionty, a database or a model client.

    Every link of `vocab.py`'s import chain is checked on its own, across all three packages, so a
    failure names the module that let something in. pydantic is allowed.
    """
    chain, _ = _chain_of("clinicalomx_markers.vocab")

    missing = [owner for owner in _OWNERS if owner not in chain]
    assert not missing, (
        f"vocab.py does not reach {missing}, so it is not aggregating and this test is back "
        "to being a fact about one file"
    )
    offenders = {}
    for link in chain:
        _, forbidden = _chain_of(link)
        if forbidden:
            offenders[link] = forbidden
    assert not offenders, "on the vocabulary chain: " + "; ".join(
        f"{link} pulled in {found}" for link, found in sorted(offenders.items())
    )


def test_core_import_does_not_reach_the_agent():
    """Importing every module, propose.llm included, imports no model client."""
    modules = [m for p in ("clinicalomx_markers", "clinicalomx_markers.sources",
                           "clinicalomx_markers.curate", "clinicalomx_markers.domain",
                           "clinicalomx_markers.query",
                           "clinicalomx_markers.propose")
               for m in _modules(p)]
    present = _in_subprocess(
        "import sys;" + "".join(f"import {m};" for m in modules)
        + f"print(','.join(m for m in {_AGENT_ONLY!r} if m in sys.modules))"
    )
    assert present == "", f"importing the package reached the agent stack: {present}"


def test_load_and_query_do_not_reach_the_agent():
    for module in (_modules("clinicalomx_markers.curate")
                   + _modules("clinicalomx_markers.domain")
                   + _modules("clinicalomx_markers.query")):
        present = _in_subprocess(
            f"import {module}, sys;"
            f"print(','.join(m for m in {_AGENT_ONLY!r} if m in sys.modules))"
        )
        assert present == "", f"{module} reached the agent stack: {present}"


def test_resolve_and_load_need_no_database():
    for module in (["clinicalomx_markers.resolve"]
                   + _modules("clinicalomx_markers.curate")
                   + _modules("clinicalomx_markers.domain")
                   + _modules("clinicalomx_markers.query")):
        present = _in_subprocess(
            f"import {module}, sys;"
            "print('yes' if 'lamindb' in sys.modules else '')"
        )
        assert present == "", f"{module} imported lamindb"


def test_the_cascade_resolves_without_a_database():
    out = _in_subprocess(
        "import sys; from clinicalomx_markers.resolve import resolve;"
        " r = resolve('NakATPase');"
        " print(f'{r.canonical},{\"lamindb\" in sys.modules}')"
    )
    canonical, lamindb_loaded = out.split(",")
    assert canonical == "NAKATPASE"
    assert lamindb_loaded == "False"
