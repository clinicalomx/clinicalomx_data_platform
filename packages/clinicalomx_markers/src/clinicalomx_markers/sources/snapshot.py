"""Store each raw fetch, so a citation points at stored bytes, not a live URL.

A snapshot is a file under a root directory; on the instance the caller registers it as an
`ln.Artifact`. `http` is the only network code here. Every caller takes a `Transport`, so
`from_mapping` serves captured fixtures and raises `LookupError` on an uncached URL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

#: Prefix of every snapshot key. Citations point at keys: changing it orphans them.
ROOT = "clinicalomx_markers/snapshots"


class SNAPSHOT_FEATURES:
    """``ln.Feature`` names a snapshot artifact carries."""

    #: When a snapshot was fetched, ISO date. Declared; nothing writes it yet.
    CLAIM_RETRIEVED_AT = "claim_retrieved_at"


@dataclass(frozen=True)
class FetchedContent:
    """A response body plus the headers a citation may need."""

    text: str
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def __call__(self, url: str) -> FetchedContent: ...


def http(url: str) -> FetchedContent:
    """Live HTTP. The only code here that touches the network."""
    import httpx

    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return FetchedContent(
        text=response.text, headers={k.lower(): v for k, v in response.headers.items()}
    )


def from_mapping(responses: dict[str, FetchedContent | str]) -> Transport:
    """Transport backed by a dict of url -> body. For tests."""

    def _transport(url: str) -> FetchedContent:
        try:
            hit = responses[url]
        except KeyError:
            raise LookupError(f"no fixture for {url}") from None
        return hit if isinstance(hit, FetchedContent) else FetchedContent(text=hit)

    return _transport


def snapshot_key(source: str, source_id: str, retrieved_at: str) -> str:
    """Stable key for one fetch, with URL slashes encoded."""
    safe = source_id.replace("://", "_").replace("/", "_")
    return f"{ROOT}/{source}/{safe}@{retrieved_at}.json"


@dataclass(frozen=True)
class Snapshot:
    """One fetch, kept verbatim. ``key`` comes from `snapshot_key` over the same three fields."""

    key: str
    source: str
    source_id: str
    retrieved_at: str
    fetched_content: FetchedContent


def write(root: Path, snapshot: Snapshot) -> str:
    path = root / snapshot.key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": snapshot.source,
        "source_id": snapshot.source_id,
        "retrieved_at": snapshot.retrieved_at,
        "headers": snapshot.fetched_content.headers,
        "body": snapshot.fetched_content.text,
    }, indent=2))
    return snapshot.key


def read(root: Path, key: str) -> Snapshot | None:
    path = root / key
    if not path.exists():
        return None
    blob = json.loads(path.read_text())
    return Snapshot(
        key=key,
        source=blob["source"],
        source_id=blob["source_id"],
        retrieved_at=blob["retrieved_at"],
        fetched_content=FetchedContent(text=blob["body"], headers=blob.get("headers", {})),
    )


def capture(root: Path, *, source: str, source_id: str, retrieved_at: str,
            fetch) -> Snapshot:
    """The stored snapshot, fetching only on a miss."""
    key = snapshot_key(source, source_id, retrieved_at)
    hit = read(root, key)
    if hit is not None:
        return hit
    snapshot = Snapshot(key=key, source=source, source_id=source_id,
                        retrieved_at=retrieved_at, fetched_content=fetch())
    write(root, snapshot)
    return snapshot
