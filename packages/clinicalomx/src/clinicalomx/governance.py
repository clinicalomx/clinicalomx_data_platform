"""Per-folder data governance policy for a storage root.

Consumers declare a :class:`Policy` per top-level folder through the ``clinicalomx.governance``
entry point. An undeclared folder resolves to :data:`UNDECLARED`, which permits nothing.

The flags form a ladder, weakest first; each requires the one below it:

``readable``  the bytes may be opened at all, for example to test a file for corruption.
``ingest``    the data may be registered as Artifacts.
``derive``    derivative outputs may be produced and stored.
``training``  human-corrected labels from it may enter a training-reference Collection.

:meth:`Policy.__post_init__` rejects a policy that breaks the ladder.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Entry-point group under which consumers advertise a ``tuple[Policy, ...]`` or a callable
#: returning one.
ENTRY_POINT_GROUP = "clinicalomx.governance"


@dataclass(frozen=True)
class Policy:
    """What may be done with the data under one top-level folder of a storage root."""

    folder: str
    readable: bool
    ingest: bool
    training: bool
    derive: bool
    #: Free text: the approval, agreement or reason behind the flags.
    basis: str = ""

    #: The ladder, weakest first. Each flag requires the one before it.
    CHAIN = (
        ("readable", None, "the bytes may be opened"),
        ("ingest", "readable", "registering reads the bytes"),
        ("derive", "ingest", "a derivative acts on catalogued data"),
        ("training", "derive", "a model trained on this data IS a derivative of it"),
    )

    def __post_init__(self) -> None:
        for name, requires, why in self.CHAIN:
            if requires and getattr(self, name) and not getattr(self, requires):
                raise ValueError(f"{self.folder!r}: {name} requires {requires} ({why})")

    @property
    def permits_nothing(self) -> bool:
        return not (self.readable or self.ingest or self.training or self.derive)


#: The policy for any folder with no explicit entry. Permits nothing.
UNDECLARED = Policy("", readable=False, ingest=False, training=False, derive=False,
                    basis="no policy declared - deny by default")


def discover() -> dict[str, Policy]:
    """``{folder: Policy}`` across every installed consumer. Raises if two declare one folder."""
    from importlib.metadata import entry_points

    out: dict[str, Policy] = {}
    origin: dict[str, str] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        declared = ep.load()
        policies = declared() if callable(declared) else declared
        for policy in policies:
            if policy.folder in out and out[policy.folder] != policy:
                raise ValueError(
                    f"folder {policy.folder!r} has conflicting policies from "
                    f"{origin[policy.folder]!r} and {ep.name!r}"
                )
            out[policy.folder] = policy
            origin[policy.folder] = ep.name
    return out


_CACHE: dict[str, Policy] | None = None

#: Policies declared at runtime through :func:`register_policies`.
_REGISTERED: dict[str, Policy] = {}


def register_policies(policies: tuple[Policy, ...] | list[Policy]) -> None:
    """Declare policies from a consumer that is not pip-installed in this runtime.

    A folder registered and discovered with different terms raises. Identical terms are a no-op.
    """
    for policy in policies:
        existing = _REGISTERED.get(policy.folder)
        if existing is not None and existing != policy:
            raise ValueError(
                f"folder {policy.folder!r} registered twice with different terms")
        _REGISTERED[policy.folder] = policy
    reset_cache()


def policies() -> dict[str, Policy]:
    """The resolved table: everything discovered plus everything registered. Memoised."""
    global _CACHE
    if _CACHE is None:
        table = discover()
        for folder, policy in _REGISTERED.items():
            if folder in table and table[folder] != policy:
                raise ValueError(
                    f"folder {folder!r} is registered with terms that differ from the installed "
                    f"declaration - two claims about the same data")
        table.update(_REGISTERED)
        _CACHE = table
    return _CACHE


def reset_cache(*, registrations: bool = False) -> None:
    """Drop the memoised policy table. ``registrations=True`` also forgets registrations (tests)."""
    global _CACHE
    _CACHE = None
    if registrations:
        _REGISTERED.clear()


def folder_of(key: str) -> str:
    """The governed folder an Artifact key belongs to (its first path segment)."""
    return (key or "").split("/")[0]


def is_hidden(key: str) -> bool:
    """Hidden and trash paths are not governed data."""
    return any(part.startswith(".") for part in key.split("/"))


def policy_for(key: str) -> Policy:
    """The policy governing an Artifact key. Undeclared folders permit nothing."""
    return policies().get(folder_of(key), UNDECLARED)


def declared_folders() -> tuple[str, ...]:
    return tuple(sorted(policies()))


def permits_read(key: str) -> bool:
    """May the bytes be opened? The weakest permission."""
    return policy_for(key).readable


def permits_ingest(key: str) -> bool:
    return policy_for(key).ingest


def permits_training(key: str) -> bool:
    """May human-corrected labels from this data enter a training-reference Collection?"""
    return policy_for(key).training


def permits_derive(key: str) -> bool:
    return policy_for(key).derive
