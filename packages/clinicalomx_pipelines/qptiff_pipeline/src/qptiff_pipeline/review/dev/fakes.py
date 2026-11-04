"""In-process stand-ins for lamindb, Prefect and the gate, for local UI work.

``install_modules()`` puts a fake ``lamindb`` and ``prefect`` in ``sys.modules`` before the app
imports them. ``patch_app()`` re-points the app's Prefect readers and gate. Everything else runs its
production code against the fake store. Writes land in ``fixtures.RECORDS`` and survive reruns.

The fake query API covers only what the app calls. Anything else raises.
"""
from __future__ import annotations

import logging
import sys
import types
from typing import ClassVar

from qptiff_pipeline.domain import keys
from qptiff_pipeline.review.dev import fixtures

log = logging.getLogger("review.dev")


# Fake lamindb.
class _Features:
    """``record.features``: reads and writes the row's ``values`` dict in place.

    Strict like the real one: a feature must be declared before it takes a value, and values must
    be JSON primitives.
    """

    def __init__(self, row: dict) -> None:
        self._row = row

    def get_values(self) -> dict:
        return {k: _wrap(v) for k, v in self._row["values"].items()}

    def add_values(self, values: dict) -> None:
        """Append, like the real one. One value reads back as a scalar, several as a list."""
        store = self._row["values"]
        for name, v in values.items():
            if name not in _FEATURES:
                raise KeyError(
                    f"feature {name!r} was never declared; call ensure_features() first "
                    "(the real lamindb would refuse this too)"
                )
            if isinstance(v, (dict, list, tuple, set)):
                raise TypeError(
                    f"feature {name!r} got a {type(v).__name__}; lamindb stores primitives, so "
                    "serialise it (json.dumps) at the boundary"
                )
            if name not in store:
                store[name] = v
                continue
            current = store[name]
            existing = current if isinstance(current, list) else [current]
            if any(_same_value(e, v) for e in existing):
                continue  # set-like: re-adding an identical value is a no-op
            store[name] = [*existing, v]
        log.info("features.add_values(%s) -> %s", self._row["name"], list(values))

    def remove_values(self, feature=None) -> None:
        """Remove one name, a list of names, or all values (None), as lamindb accepts."""
        store = self._row["values"]
        names = list(store) if feature is None else (
            [feature] if isinstance(feature, str) else list(feature)
        )
        for name in names:
            store.pop(name, None)
        log.info("features.remove_values(%s, %s)", self._row["name"], names)


def _same_value(a, b) -> bool:
    """Equality for the set-like add: Artifacts compare by key, everything else by value."""
    if a is b:
        return True
    ak, bk = getattr(a, "key", None), getattr(b, "key", None)
    if ak is not None or bk is not None:
        return ak == bk
    try:
        return bool(a == b)
    except Exception:
        return False


class _Artifact:
    """Stand-in for ``ln.Artifact``, interned by key, with a ``features`` manager."""

    # Shared across instances: this is the intern table.
    _interned: ClassVar[dict] = {}

    def __new__(cls, key: str, **_kw):
        if key in cls._interned:
            return cls._interned[key]
        self = super().__new__(cls)
        self.key = key
        self.uid = f"art-{keys.slug(key)}"[:20]
        self.created_at = None
        self._values: dict = {}
        cls._interned[key] = self
        return self

    def __init__(self, key: str, **_kw) -> None:  # __new__ did the work
        pass

    @property
    def features(self) -> _Features:
        return _Features({"name": self.key, "values": self._values})

    def __repr__(self) -> str:
        return f"Artifact({self.key!r})"

    @classmethod
    def get(cls, key: str | None = None, **kw):
        return cls(key or kw.get("key") or "unknown")

    @classmethod
    def filter(cls, **kw):
        key = kw.get("key")
        return _QuerySet([cls(str(key))] if key else [])


def _wrap(value):
    """Feature values tagged ``{"__artifact__": key}`` in the fixtures become Artifacts."""
    if isinstance(value, dict) and "__artifact__" in value:
        return _Artifact(value["__artifact__"])
    return value


class _Record:
    """A row of ``fixtures.RECORDS``, or a type Record (``is_type=True``)."""

    def __init__(self, row: dict | None = None, *, type_name: str | None = None) -> None:
        self._row = row
        self._type_name = type_name

    # Type records (ln.Record.filter(name=..., is_type=True)).
    @property
    def is_type(self) -> bool:
        return self._row is None

    @property
    def name(self) -> str:
        return self._type_name if self._row is None else self._row["name"]

    @property
    def uid(self) -> str:
        return f"type-{self._type_name}" if self._row is None else self._row["uid"]

    @property
    def type(self):
        return None if self._row is None else _Record(type_name=self._row["type"])

    @property
    def features(self) -> _Features:
        if self._row is None:
            raise TypeError("a type Record has no features")
        return _Features(self._row)

    def __repr__(self) -> str:
        return f"Record({self.name!r})"

    # Query API.
    @classmethod
    def get(cls, uid: str | None = None, **kw):
        uid = uid or kw.pop("uid", None)
        row = fixtures.RECORDS.get(uid)
        if row is None:
            raise DoesNotExist(f"no fake Record with uid={uid!r}")
        return cls(row)

    @classmethod
    def filter(cls, **kw) -> _QuerySet:
        known = {"name", "is_type", "type", "name__startswith", "uid"}
        if unknown := set(kw) - known:
            raise NotImplementedError(f"fake Record.filter does not support {sorted(unknown)}")
        if "uid" in kw:  # clinicalomx.roi.resolve_roi looks an ROI up by its uid back-ref
            row = fixtures.RECORDS.get(kw["uid"])
            return _QuerySet([cls(row)] if row else [])
        if kw.get("is_type"):  # the type record itself
            name = kw.get("name")
            types_ = {r["type"] for r in fixtures.RECORDS.values()}
            return _QuerySet([cls(type_name=name)] if name in types_ else [])
        want_type = kw.get("type")
        type_name = want_type.name if isinstance(want_type, _Record) else want_type
        out = []
        for row in fixtures.RECORDS.values():
            if type_name is not None and row["type"] != type_name:
                continue
            if "name" in kw and row["name"] != kw["name"]:
                continue
            if "name__startswith" in kw and not row["name"].startswith(kw["name__startswith"]):
                continue
            out.append(cls(row))
        return _QuerySet(out)


class DoesNotExist(Exception):
    """Raised by ``Record.get`` for an unknown uid, as lamindb does."""


class _QuerySet(list):
    def one(self):
        if len(self) != 1:
            raise DoesNotExist(f"expected exactly 1 record, got {len(self)}")
        return self[0]

    def one_or_none(self):
        if len(self) > 1:
            raise DoesNotExist(f"expected 0 or 1 record, got {len(self)}")
        return self[0] if self else None

    def first(self):
        return self[0] if self else None

    def order_by(self, *_fields):
        """No-op: the fake holds at most one artifact per key."""
        return self

    def to_dataframe(self, features: list[str] | None = None, limit: int | None = None):
        """uid, name and the requested feature columns, as ``_unit_states`` reads them."""
        import pandas as pd

        rows = []
        for rec in list(self)[:limit]:
            vals = rec.features.get_values()
            rows.append({"uid": rec.uid, "name": rec.name,
                         **{f: vals.get(f) for f in (features or [])}})
        return pd.DataFrame(rows, columns=["uid", "name", *(features or [])])


# The declared-feature registry: real membership. Seeded from the fixtures, then grown by
# ensure_features and Feature().save().
_FEATURES: dict[str, object] = {}


class _Feature:
    """``ln.Feature``: get-or-create over :data:`_FEATURES`. Accepts the real registry's kwargs."""

    def __init__(self, name: str | None = None, dtype=None, **kwargs) -> None:
        self.name, self.dtype = name, dtype
        self.description = kwargs.get("description")
        self._dtype_str = str(dtype)  # the reconciler compares and repairs through this

    @classmethod
    def filter(cls, **kw) -> _QuerySet:
        name = kw.get("name")
        return _QuerySet([cls(name, _FEATURES[name])] if name in _FEATURES else [])

    def save(self):
        _FEATURES[self.name] = self.dtype
        log.info("[fake lamindb] declared feature %s (%s)", self.name, self.dtype)
        return self


def _fake_lamindb() -> types.ModuleType:
    m = types.ModuleType("lamindb")
    m.Record = _Record
    m.Artifact = _Artifact
    m.Feature = _Feature
    m.DoesNotExist = DoesNotExist
    setup = types.SimpleNamespace(
        settings=types.SimpleNamespace(instance=types.SimpleNamespace(slug="dev/fake-instance"))
    )
    m.setup = setup
    return m


# Fake prefect: only the deployment-trigger paths. The readers are patched instead.
class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_deployment_by_name(self, name: str):
        log.info("[fake prefect] read_deployment_by_name(%s)", name)
        return types.SimpleNamespace(id=f"dep-{keys.slug(name)}", name=name)

    def create_flow_run_from_deployment(self, dep_id, parameters: dict | None = None):
        log.info("[fake prefect] create_flow_run_from_deployment(%s, %s)", dep_id, parameters)
        return types.SimpleNamespace(name=f"fake-run-{dep_id[-6:]}", id="fake-run-id")

    def read_artifacts(self, **kw):
        return []


def _fake_prefect() -> dict[str, types.ModuleType]:
    """The ``prefect.*`` submodules the app imports lazily, as callable stubs."""
    mods: dict[str, types.ModuleType] = {}

    def mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        mods[name] = m
        return m

    mod("prefect")
    mod("prefect.client")
    orch = mod("prefect.client.orchestration")
    orch.get_client = lambda *a, **kw: _FakeClient()
    mod("prefect.client.schemas")
    filt = mod("prefect.client.schemas.filters")
    filt.ArtifactFilter = lambda **kw: None
    filt.ArtifactFilterKey = lambda **kw: None
    srt = mod("prefect.client.schemas.sorting")
    srt.ArtifactSort = types.SimpleNamespace(CREATED_DESC="CREATED_DESC")
    events = mod("prefect.events")
    events.emit_event = lambda **kw: log.info("[fake prefect] emit_event(%s)", kw.get("event"))
    return mods


# Fake gate and fake Prefect-artifact readers.
class _FakeGate:
    """``gate.decide`` and ``gate.reopen`` against the fake store. State moves, no events."""

    def decide(self, stage: str, unit_uid: str, decision, payload: dict | None = None) -> dict:
        from clinicalomx import set_state

        from qptiff_pipeline.domain import states

        g = states.GATES[stage]
        target = g.approved if str(getattr(decision, "value", decision)) == "accept" else g.rejected
        rec = _Record.get(uid=unit_uid)
        set_state(rec, target)
        log.info("[fake gate] %s %s %s -> %s (payload=%s)", stage, rec.name, decision, target, payload)
        return {"unit": rec.name, "state": target, "decision": str(decision), "fake": True}

    def reopen(self, stage: str, unit_uid: str, cascade: bool = False) -> dict:
        from clinicalomx import set_state

        from qptiff_pipeline.domain import states

        rec = _Record.get(uid=unit_uid)
        target = states.GATES[stage].pending
        set_state(rec, target)
        log.info("[fake gate] reopen %s %s -> %s (cascade=%s)", stage, rec.name, target, cascade)
        return {"unit": rec.name, "state": target, "fake": True,
                "superseded_tmas": [], "resetting_tmas": []}


def _fake_load_bundle(edit_prefix: str, name: str) -> dict | None:
    return fixtures.BUNDLES.get((edit_prefix, keys.slug(name)))


def _fake_qc_rows(core_name: str) -> list[dict] | None:
    return fixtures.QC_ROWS.get(core_name)


# Install.
def install_modules() -> None:
    """Put the fake lamindb and prefect in ``sys.modules``, before the app imports them."""
    if not getattr(sys.modules.get("lamindb"), "__fake__", False):
        m = _fake_lamindb()
        m.__fake__ = True
        sys.modules["lamindb"] = m
    if not getattr(sys.modules.get("prefect"), "__fake__", False):
        for name, mod in _fake_prefect().items():
            mod.__fake__ = True
            sys.modules[name] = mod
    if not fixtures.RECORDS:
        fixtures.build()
    _declare_fixture_features()


def _declare_fixture_features() -> None:
    """Register every feature the fixtures seed. Features the app declares itself are left out, so
    a missing ``ensure_features`` in app code still fails."""
    for row in fixtures.RECORDS.values():
        for name in row["values"]:
            _FEATURES.setdefault(name, None)


def declared_features() -> dict:
    """The fake Feature registry, for tests asserting a flow declared what it wrote."""
    return dict(_FEATURES)


def _fake_vocab_ensure(*, dry_run: bool = False, force: bool = False):
    """Stand-in for ``vocab.ensure()``: register the declared names, skip the reconcile."""
    from qptiff_pipeline.domain import vocab as _vocab

    for name in _vocab.declared_names():
        _FEATURES.setdefault(name, None)
    log.info("[fake vocab] declared %d feature(s) (reconcile skipped)", len(_vocab.declared_names()))


def patch_app(app) -> None:
    """Re-point the app's backend functions at the fakes. Call after every reload."""
    from qptiff_pipeline.domain import vocab

    app._fresh_db = lambda: None  # no Django connection pool
    app.load_bundle = _fake_load_bundle          # Prefect edit-bundle artifact
    app._bgnorm_qc_rows = _fake_qc_rows          # Prefect QC table artifact
    app.gate = _FakeGate()                       # the decision seam
    # Patched on the module: every caller reads `vocab.ensure` through it.
    vocab.ensure = _fake_vocab_ensure


def reset_data() -> None:
    """Rebuild the fake world, discarding every write made this session."""
    _Artifact._interned.clear()  # else ROI back-refs written before the reset survive it
    fixtures.build()
