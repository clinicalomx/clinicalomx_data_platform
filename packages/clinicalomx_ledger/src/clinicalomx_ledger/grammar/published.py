"""A release as a file: the approved claims, readable with no database.

`services.export_release` writes it; `load` reads it with only the standard library and pydantic.
Entries are claims whose newest decision accepted them when the release was cut, in decision
order. The bytes are reproducible: sorted keys, no export timestamp. The header carries a
manifest hash over the decision uids; it does not cover edited values inside an entry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

#: The format a file declares. A reader refuses any other.
FORMAT = "clinicalomx-ledger-release/1"


def manifest_hash(decision_uids) -> str:
    """The digest a release is published under: sha256 of its sorted decision uids."""
    return hashlib.sha256("\n".join(sorted(decision_uids)).encode()).hexdigest()


@dataclass(frozen=True)
class Answer:
    """One approved answer: the claim, and the accepting decision that made it one."""

    subject: str
    predicate: str
    value: str
    source: str
    source_id: str
    source_version: str
    evidence: str
    claim_uid: str
    decision_uid: str
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class Published:
    """A release as a reader holds it. `answers` is in decision order."""

    release_uid: str
    domain: str
    manifest_hash: str
    released_at: str
    released_by: str
    policy_version: str
    answers: tuple[Answer, ...]


def render(published: Published) -> bytes:
    """The canonical bytes of ``published``."""
    body = {
        "format": FORMAT,
        "release": {
            "uid": published.release_uid,
            "domain": published.domain,
            "manifest_hash": published.manifest_hash,
            "released_at": published.released_at,
            "released_by": published.released_by,
            "policy_version": published.policy_version,
        },
        "answers": [asdict(a) for a in published.answers],
    }
    return (json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def load(source: bytes | str | Path) -> Published:
    """Read an export back from bytes or a path.

    Refuses a foreign format or entries whose decision uids do not hash to the manifest.
    An edited value inside an entry still loads.
    """
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    body = json.loads(raw)
    if body.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} export: it declares {body.get('format')!r}")
    head = body["release"]
    answers = tuple(Answer(**a) for a in body["answers"])
    found = manifest_hash(a.decision_uid for a in answers)
    if found != head["manifest_hash"]:
        raise ValueError(
            f"export of release {head['uid']!r} declares manifest {head['manifest_hash']} "
            f"but its entries hash to {found}, so it is not that release"
        )
    return Published(
        release_uid=head["uid"],
        domain=head["domain"],
        manifest_hash=head["manifest_hash"],
        released_at=head["released_at"],
        released_by=head["released_by"],
        policy_version=head["policy_version"],
        answers=answers,
    )
