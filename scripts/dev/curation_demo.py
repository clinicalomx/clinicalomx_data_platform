"""Curate a real spreadsheet on a throwaway instance. Run it through `curation-demo.sh`.

"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
CURATION_APP = REPO / "packages" / "clinicalomx" / "curation_app"
BRANCH = "curation"
POLICY = "curation-demo-policy"
CURATOR = "curator"
AGENT = "column-rules"
READY_SECONDS = 900

sys.path.insert(0, str(REPO / "packages" / "clinicalomx_ledger"))
import disposable


def say(text: str) -> None:
    print(f"==> {text}", flush=True)


def port_is_held(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    # A TERM takes the Ctrl-C path, so the page is stopped and the container removed either way.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sheet", type=Path, help="the .xlsx or .csv to curate")
    parser.add_argument("--sheet-name", help="which sheet of an .xlsx; the first by default")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8602")))
    args = parser.parse_args()

    sheet = args.sheet.expanduser().resolve()
    if not sheet.is_file():
        print(f"no such file: {sheet}", file=sys.stderr)
        return 2
    if port_is_held(args.port):
        print(f"port {args.port} already answers; stop what holds it or pass --port",
              file=sys.stderr)
        return 2

    caches = reflex_caches()
    say("starting a throwaway postgres:18 container and a lamin instance on it "
        "(the first run pulls the image; about a minute)")
    made = disposable.start()
    say(f"instance ready in {made.workdir}")

    with disposable.quiet_lamindb_import():
        import lamindb as ln
    disposable.verify_isolated()
    db = urlsplit(made.db_url)
    database = f"{db.hostname}:{db.port}{db.path}"
    say(f"isolation checked: Django is connected to the throwaway container at {database}")

    from clinicalomx.review import ensure_state_feature
    from clinicalomx_ledger import services
    from clinicalomx_markers import __version__
    from clinicalomx_markers.api.tokens import Tokens, digest
    from clinicalomx_markers.api.v1 import curation_api
    from clinicalomx_markers.domain.reagent_mapping import (
        MAPPING_TYPES,
        REAGENT_MAPPING_DOMAIN,
        ReagentMappingPredicate,
    )
    from clinicalomx_markers.propose.columns import (
        column_profile,
        map_columns,
        source_profile,
    )
    from clinicalomx_markers.sources.sheet import columns_of, read_rows
    from starlette.testclient import TestClient

    say(f"registering {sheet.name} as an artifact on the throwaway instance")
    ln.Branch(name=BRANCH).save()
    ensure_state_feature(feature=REAGENT_MAPPING_DOMAIN.state_feature)
    # No "/" in the key: it is the claims' subject, and the lineage route takes it as one
    # path segment.
    artifact = ln.Artifact(sheet, key=sheet.name,
                           description="curation demo source").save()
    kind = ln.Record(name=MAPPING_TYPES.ARTIFACT, is_type=True).save()
    ln.Record(name=artifact.key, type=kind, branch=ln.Branch.get(name=BRANCH)).save()

    say(f"reading {sheet.name} and proposing which column fills which reagent field")
    # The cached copy is named by its uid, and a .csv's sheet name is its file stem.
    csv_name = sheet.stem if sheet.suffix.lower() == ".csv" else None
    sheet_name, rows = read_rows(artifact.cache(), sheet=args.sheet_name or csv_name)
    columns = columns_of(rows, sheet=sheet_name)
    mapping = map_columns(artifact.key, columns, source_version=artifact.uid)
    say(f"read sheet {sheet_name!r}: {len(rows)} row(s) including the header, "
        f"{len(columns)} column(s)")
    for claim in mapping.claims:
        print(f"    {claim.value:<36} -> {claim.predicate}  (confidence {claim.confidence})")
    for left in mapping.unplaced:
        print(f"    {left.column.address:<12} no claim: {left.reason}")

    tokens = {CURATOR: secrets.token_urlsafe(24), AGENT: secrets.token_urlsafe(24)}
    tokens_file = made.workdir / "tokens.json"
    tokens_file.write_text(json.dumps({
        digest(tokens[CURATOR]): {"actor": CURATOR, "kind": "human"},
        digest(tokens[AGENT]): {"actor": AGENT, "kind": "agent", "agent_model": AGENT,
                                "agent_model_version": __version__,
                                "policy_version": POLICY},
    }))
    services.approve_policy(version=POLICY, approved_by=CURATOR)
    say(f"minted a curator token and an agent token; approved policy {POLICY!r} for the agent")

    api = TestClient(curation_api(tokens=Tokens.load(tokens_file), branch=BRANCH))
    agent = {"Authorization": f"Bearer {tokens[AGENT]}"}
    # Counted once, by the reader that built the claims' evidence; the page shows these numbers.
    sheet_profile = source_profile(mapping, file=sheet.name, content_hash=artifact.hash or "")
    for column, proposal, claim in mapping.placed:
        posted = api.post("/api/v1/proposals", headers=agent, json={
            "domain": REAGENT_MAPPING_DOMAIN.name, "subject": claim.subject,
            "predicate": str(claim.predicate), "value": claim.value,
            "evidence": {"source": str(claim.source), "source_id": claim.source_id,
                         "source_version": claim.source_version, "text": claim.evidence},
            "confidence": claim.confidence, "reasoning": proposal.reasoning,
            "reasons": [r.to_json() for r in proposal.reasons],
            "alternatives": [{"value": a.value, "confidence": a.confidence,
                              "reasoning": a.reasoning} for a in proposal.alternatives],
            "column": column_profile(column, None if proposal.transform else proposal.value),
            "source_profile": sheet_profile,
            "triage": {"label": proposal.triage.label.value,
                       "reason": proposal.triage.reason}})
        if posted.status_code not in (200, 201):
            print(f"the API refused {claim.value}: {posted.status_code} {posted.text}",
                  file=sys.stderr)
            return 1
    queue = api.get("/api/v1/queue", params={"limit": 500},
                    headers={"Authorization": f"Bearer {tokens[CURATOR]}"}).json()["items"]
    say(f"proposed through the API as {AGENT!r}; {len(queue)} claim(s) waiting for review")
    if not queue:
        why = ("the sheet has no header row and no data" if not columns else
               "no column's header or values named a reagent field "
               f"({', '.join(ReagentMappingPredicate)})")
        say(f"ZERO CLAIMS from {sheet.name}, sheet {sheet_name!r}, {len(columns)} column(s): "
            f"{why}. The page will open on an empty queue for that reason")

    url = f"http://localhost:{args.port}"
    env = app_env(caches, tokens_file=tokens_file, database=database, url=url)
    say(f"building and starting the review page on {url}; the first build fetches the page's "
        "npm packages and can take several minutes. Reflex's own progress follows")
    reflex = Path(sys.executable).parent / "reflex"
    app = subprocess.Popen([str(reflex), "run", "--env", "prod", "--frontend-port",
                            str(args.port), "--backend-port", str(args.port)],
                           cwd=CURATION_APP, env=env)
    try:
        if not wait_for_api(url, tokens[CURATOR], app):
            print("the page did not come up; Reflex's output above says why", file=sys.stderr)
            return 1
        print(f"""
------------------------------------------------------------------------------
The curation page is up.

  open            {url}
  curator token   {tokens[CURATOR]}
                  paste it into the token box; the page checks it and reads the queue
  agent token     {tokens[AGENT]}
                  for the refusals, e.g. an agent may not cut a release (403):
                  curl -s -X POST {url}/api/v1/releases -H 'Authorization: Bearer {tokens[AGENT]}' \\
                    -H 'Content-Type: application/json' -d '{{"domain":"{REAGENT_MAPPING_DOMAIN.name}"}}'
  waiting         {len(queue)} claim(s) from sheet {sheet_name!r} of {sheet.name}

To throw the whole instance away, press Ctrl-C here: the app stops and the
container is removed. Then delete {made.workdir}.
If this terminal dies first: docker rm -f {made.container_id[:12]}
------------------------------------------------------------------------------
""", flush=True)
        return app.wait()
    except KeyboardInterrupt:
        say("stopping the page and removing the throwaway container")
        return 0
    finally:
        if app.poll() is None:
            app.terminate()
            app.wait()


def reflex_caches() -> dict[str, str]:
    """Reflex's data dir and bun's install cache under the operator's HOME, read before the redirect."""
    from platformdirs import PlatformDirs

    return {"REFLEX_DIR": str(PlatformDirs("reflex", False).user_data_path),
            "BUN_INSTALL_CACHE_DIR": str(Path.home() / ".bun" / "install" / "cache")}


def app_env(caches: dict[str, str], *, tokens_file: Path, database: str,
            url: str) -> dict[str, str]:
    """The app's environment: the redirected HOME and lamin settings, plus only the two caches."""
    return {**os.environ, **caches, "CURATION_TOKENS": str(tokens_file),
            "CURATION_BRANCH": BRANCH, "CURATION_REQUIRE_DATABASE": database,
            "CURATION_API_URL": url}


def wait_for_api(url: str, token: str, app: subprocess.Popen) -> bool:
    """True once the served app answers the queue with JSON, the check the page itself needs."""
    import httpx

    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if app.poll() is not None:
            return False
        try:
            got = httpx.get(f"{url}/api/v1/queue", params={"limit": 1},
                            headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if got.headers.get("content-type", "").startswith("application/json"):
                return got.status_code == 200
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


if __name__ == "__main__":
    sys.exit(main())
