"""Every read this reviewer makes from the instance, all through explorer/lamin_api.

Calls run one after another, the number of sampled registries is capped, and nothing polls.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

EXPLORER = Path(__file__).resolve().parent.parent / "explorer"

# Registries sampled when none are named.
DEFAULT_REGISTRIES = ("Artifact", "Record")

# Each sampled registry costs several queries.
MAX_REGISTRIES = 8

MAX_STORAGES = 200

# SQLSTATE insufficient_privilege: "permission denied for table X".
PERMISSION_DENIED = "42501"


def _api():
    """lamin_api, imported late: importing it connects to the host's configured instance."""
    if str(EXPLORER) not in sys.path:
        sys.path.insert(0, str(EXPLORER))
    import lamin_api

    return lamin_api


def _permission_refusal(exc: BaseException, role: str, consequence: str) -> str | None:
    """The refusal text for a read the database denied, or None.

    Matches SQLSTATE 42501 anywhere in the exception chain, since Django wraps the driver's
    error. consequence says what this read cost.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = str(exc).strip()
        detail = text.splitlines()[0] if text else ""
        if getattr(exc, "pgcode", None) == PERMISSION_DENIED or "permission denied for" in detail:
            return (
                f"the database refused a read as role {role or 'unknown'}: {detail}. "
                f"{consequence} Grant that role SELECT on the instance's tables, and ALTER "
                "DEFAULT PRIVILEGES so a later migration's tables are readable too, or connect "
                "as a role that already has it.")
        exc = exc.__cause__ or exc.__context__
    return None


@contextmanager
def _guarded(role: str, consequence: str):
    """Relabel a denied read as a refusal naming the grant. Other errors pass through unchanged."""
    try:
        yield
    except Exception as exc:
        refusal = _permission_refusal(exc, role, consequence)
        if refusal is None:
            raise
        raise RuntimeError(refusal) from exc


def landed() -> dict:
    """The instance as it stands: features, entity types, storages, registry counts, connection.

    Refuses when nothing is connected or a read is denied.
    """
    api = _api()
    slug = api.instance_slug()
    if not slug:
        raise RuntimeError(
            "no lamindb instance is connected, so every declared name would be reported as one "
            "that did not land. Connect one with make lamin-connect, or make lamin-local if you "
            "have no route to the control-plane VM, or name the host ~/.lamin with LAMIN_HOME "
            "on that VM.")
    connection = api.instance_connection()
    with _guarded(connection.get("role", ""),
                  "Every declared name would otherwise be reported as one that did not land."):
        features = api.list_features()
        types = api.list_entity_types()
        registries = api.list_registries()
        storages = _storages(api)
    return {
        "instance": slug,
        "connection": connection,
        "features": features,
        "types": types,
        "registries": registries,
        "storages": storages,
    }


def _storages(api) -> list[dict]:
    """Storage roots, read through lamin_api.run_query."""
    payload = api.run_query("Storage", [], False, MAX_STORAGES)
    columns = payload["columns"]
    # Matching is on root; a frame without it is an error.
    if "root" not in columns:
        raise RuntimeError(
            "the Storage query returned no root column, so no storage root can be compared; "
            f"columns were: {', '.join(columns) or 'none'}")
    if payload.get("truncated"):
        raise RuntimeError(
            f"the Storage query hit its cap of {MAX_STORAGES} rows, so a declared root past the "
            "cap would be reported as one that did not land")
    pick = lambda row, name: row[columns.index(name)] if name in columns else ""
    # The landed label is the description: Storage has no field meaning the declaration's key.
    return [{"uid": pick(row, "uid"), "root": pick(row, "root"),
             "description": pick(row, "description")}
            for row in payload["rows"]]


def rules():
    """lamin_api's feature_kind and scalar, by reference."""
    api = _api()
    return api.feature_kind, api.scalar


def choose_registries(registries: list[dict], asked: list[str] | None) -> tuple[list[str],
                                                                              list[str]]:
    """Which registries to sample, and which named ones the cap left out.

    An explicit list is taken as given. Otherwise the defaults are used, minus empty ones.
    """
    if asked:
        return asked[:MAX_REGISTRIES], asked[MAX_REGISTRIES:]
    counts = {r["name"]: r.get("count") or 0 for r in registries}
    populated = [name for name in DEFAULT_REGISTRIES if counts.get(name)]
    return populated[:MAX_REGISTRIES], populated[MAX_REGISTRIES:]


def sample(registry: str, limit: int) -> dict:
    """A bounded page of rows with every in-scope feature merged on as a column.

    A denied read refuses like landed() does, saying this registry's rows were not read.
    """
    api = _api()
    role = api.instance_connection().get("role", "")
    # An unknown registry name reads as a typo, not a traceback.
    try:
        with _guarded(role,
                      "The declared versus landed comparison is already read, and it is the "
                      f"sampled rows for {registry} that cannot be rendered."):
            payload = api.run_query(registry, [], True, limit)
    except LookupError as exc:
        raise RuntimeError(
            f"{exc}. --registry, or SCHEMA_REVIEW_REGISTRIES, named a registry this instance does "
            f"not have; naming none samples {' and '.join(DEFAULT_REGISTRIES)}.") from exc
    payload["registry"] = registry
    return payload
