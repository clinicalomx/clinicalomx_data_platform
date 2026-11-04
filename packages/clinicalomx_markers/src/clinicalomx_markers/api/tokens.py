"""Bearer tokens, each naming one actor and its kind, human or agent. Tokens carry no
permissions; the ledger enforces what an agent may do.

The token file maps SHA-256 digests, never token values, to actors:

    {"<sha256 hex of the token>": {"actor": "rafael", "kind": "human"},
     "<sha256 hex>": {"actor": "curator-agent", "kind": "agent",
                      "agent_model": "claude-opus-5-5", "agent_model_version": "2026-06",
                      "policy_version": "marker-roles-v1"}}

`digest` computes a key for that file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Actor:
    """Who a token is, typed as the ledger types every row."""

    name: str
    #: ``human`` or ``agent``, the ledger's `ActorKind`.
    kind: str
    agent_model: str = ""
    agent_model_version: str = ""
    #: The policy an agent acts under. The ledger records whether a human approved it.
    policy_version: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an actor names itself; the name is blank")
        if self.kind not in ("human", "agent"):
            raise ValueError(f"{self.name}: an actor is human or agent, not {self.kind!r}")

    def session(self) -> dict[str, str]:
        """The `LedgerClaimStore` keywords this actor writes under."""
        return {"actor": self.name, "actor_kind": self.kind, "agent_model": self.agent_model,
                "agent_model_version": self.agent_model_version,
                "policy_version": self.policy_version}


def digest(token: str) -> str:
    """The key a token is filed under."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Tokens:
    """Digest -> actor. Looking a token up is the whole of authentication."""

    actors: dict[str, Actor]

    def actor(self, token: str) -> Actor | None:
        """The actor ``token`` names, or None. Compared in constant time."""
        wanted = digest(token)
        for key, actor in self.actors.items():
            if hmac.compare_digest(key, wanted):
                return actor
        return None

    @classmethod
    def load(cls, path: str | Path) -> Tokens:
        """Read the digest file described in the module docstring."""
        raw = json.loads(Path(path).read_text())
        return cls({key: Actor(name=entry["actor"], kind=entry["kind"],
                               agent_model=entry.get("agent_model", ""),
                               agent_model_version=entry.get("agent_model_version", ""),
                               policy_version=entry.get("policy_version", ""))
                    for key, entry in raw.items()})
