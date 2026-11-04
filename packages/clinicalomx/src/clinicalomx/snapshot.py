"""Cached bulk reads of the instance: one query per type, never one per record.

Read-only. lamindb is imported inside functions, never at module scope.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from clinicalomx.names import FEATURES, TYPES

#: Sentinel for "never loaded"; ``None`` means loaded with all features.
_UNLOADED = object()

#: :meth:`Snapshot._columns_to_load` result meaning "already cached, read nothing".
_COVERED = object()

#: Features a record inherits from its ancestors when it has none of its own.
INHERITED_FEATURES = ("source_image",)

#: Keys per ``key__in`` round trip.
_CHUNK = 500


@dataclass
class Snapshot:
    """Lazily built, memoised bulk reads of the instance, shared by every reader in one run."""

    #: Columns the automatic ROI-subtype pass reads. ``None`` means every feature.
    roi_columns: Sequence[str] | None = None

    _records: dict[str, list] = field(default_factory=dict)
    _states: dict[str, dict[str, str]] = field(default_factory=dict)
    _values: dict[str, dict] = field(default_factory=dict)
    #: ROI subtypes whose feature values have been bulk-loaded; ``None`` until the first load.
    _values_loaded: list[str] | None = None
    _roi_types: list[str] | None = None
    #: Every feature name on the instance, read once.
    _feature_names: set[str] | None = None
    #: ``{type name: features loaded}``; ``None`` means all. Absent means never loaded.
    _loaded_features: dict[str, set[str] | None] = field(default_factory=dict)
    _role_features: tuple[str, ...] | None = None
    _provenance: tuple[str, ...] | None = None
    #: Artifact-dtype features THE INSTANCE declares, read alongside _feature_names.
    _artifact_features: tuple[str, ...] | None = None
    _role_artifacts: list[tuple[object, str, str]] | None = None
    _backrefs: dict[str, dict] | None = None
    #: ``{type name: {record name: record}}``, built from :meth:`records`.
    _by_name: dict[str, dict[str, object]] = field(default_factory=dict)
    #: The ROI graph, built once by :meth:`_build_graph`.
    _by_uid: dict[str, object] | None = None
    _parent_of: dict[str, str] | None = None
    _children_of: dict[str, list[str]] | None = None
    #: Ambiguities found while indexing. Reported by callers, never raised.
    _graph_warnings: list[str] = field(default_factory=list)
    #: ``{record uid: {role feature: (artifact key, ...)}}``, inverted from :meth:`role_artifacts`.
    _roles_by_uid: dict[str, dict[str, tuple[str, ...]]] | None = None
    #: When the first bulk read hit the instance.
    _read_at: str | None = None

    @property
    def read_at(self) -> str | None:
        """When the first bulk read hit the instance, or ``None`` if nothing was read.

        Timestamp anything derived from this Snapshot with this value.
        """
        return self._read_at

    def _mark_read(self) -> None:
        if self._read_at is None:
            self._read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- bulk reads ---------------------------------------------------------------------

    def records(self, type_name: str) -> list:
        """Every Record of a type. One query, memoised per type."""
        if type_name in self._records:
            return self._records[type_name]  # before the import: a hit must not even connect

        import lamindb as ln

        self._mark_read()

        self._records[type_name] = ln.Record.filter(type__name=type_name).to_list()
        return self._records[type_name]

    def states(self, type_name: str) -> dict[str, str]:
        """``{record uid: pipeline_state}`` for a type, as one dataframe read."""
        if type_name in self._states:
            return self._states[type_name]

        import lamindb as ln

        self._mark_read()

        col = FEATURES.PIPELINE_STATE
        df = ln.Record.filter(type__name=type_name).to_dataframe(
            features=[col], limit=1_000_000)
        self._states[type_name] = (
            dict(zip(df["uid"], df[col], strict=True)) if col in df.columns else {}
        )
        return self._states[type_name]

    def values(self, record) -> dict:
        """A record's feature values, from one bulk read per ROI type.

        A record that is not an ROI subtype falls back to a per-record read.
        """
        if self._values_loaded is None:
            self._load_roi_values()
        uid = record.uid
        if uid not in self._values:
            self._values[uid] = record.features.get_values()
        return self._values[uid]

    def _load_roi_values(self) -> None:
        """Bulk-load every ROI subtype, one dataframe each, narrowed by :attr:`roi_columns`."""
        if self._values_loaded is None:
            self._values_loaded = []
        for type_name in self.roi_types():
            self.load_values(type_name, features=self.roi_columns)

    def _columns_to_load(self, type_name: str, features: Sequence[str] | None):
        """What this call must read: :data:`_COVERED`, ``None`` for all, or a column set.

        A covered request reads nothing; any other reads the union of cached and requested.
        """
        want = None if features is None else set(features)
        loaded = self._loaded_features.get(type_name, _UNLOADED)
        if loaded is not _UNLOADED and (loaded is None or (want is not None and want <= loaded)):
            return _COVERED
        if loaded is not _UNLOADED and not (loaded is None or want is None):
            return want | loaded
        return want

    def load_values(self, type_name: str, features: Sequence[str] | None = None) -> None:
        """Bulk-load every record of ``type_name`` into the value cache. Idempotent.

        Call before iterating a non-ROI type; :meth:`values` falls back to one query per record.
        ``features`` narrows the read; an uncovered request re-reads the union. Null cells are
        dropped, so an absent key means no value, as with ``get_values()``.
        """
        want = self._columns_to_load(type_name, features)
        if want is _COVERED:
            return  # before the import: a covered caller must cost nothing, not even a connect

        import lamindb as ln

        self._mark_read()

        if self._values_loaded is None:
            self._values_loaded = []
        if self._feature_names is None:
            self._load_feature_dtypes()
        df = ln.Record.filter(type__name=type_name).to_dataframe(
            features=True if want is None else sorted(want), limit=1_000_000)
        columns = [c for c in df.columns if c in self._feature_names]
        for row in df.itertuples(index=False):
            as_dict = row._asdict()
            self._values[as_dict["uid"]] = {
                c: value for c in columns
                if (value := _present(as_dict.get(c))) is not None
            }
        self._loaded_features[type_name] = want
        if type_name not in self._values_loaded:
            self._values_loaded.append(type_name)

    def role_artifacts(self) -> list[tuple[object, str, str]]:
        """Every ``(roi_record, role_feature, artifact_key)`` triple on the instance. Memoised."""
        if self._role_artifacts is not None:
            return self._role_artifacts

        roles = self.role_features()
        out: list[tuple[object, str, str]] = []
        for type_name in self.roi_types():
            for rec in self.records(type_name):
                values = self.values(rec)
                for feature in roles:
                    value = values.get(feature)
                    if value is None:
                        continue
                    many = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
                    out.extend((rec, feature, _key_of(v)) for v in many if v is not None)
        self._role_artifacts = out
        return out

    def artifact_rows(self, keys: Iterable[str], columns: Sequence[str]) -> dict[str, dict]:
        """``{artifact key: {column: value}}`` for the given keys, chunked. Not memoised."""
        import lamindb as ln

        self._mark_read()

        wanted = sorted(set(columns))
        ordered = sorted({k for k in keys if k})
        found: dict[str, dict] = {}
        for start in range(0, len(ordered), _CHUNK):
            chunk = ordered[start:start + _CHUNK]
            # limit= is mandatory: to_dataframe defaults to 20 rows and only warns.
            df = ln.Artifact.filter(key__in=chunk).to_dataframe(
                features=wanted, limit=len(chunk))
            for row in df.itertuples(index=False):
                as_dict = row._asdict()
                found[as_dict["key"]] = {
                    c: value for c in wanted
                    if (value := _present(as_dict.get(c))) is not None
                }
        return found

    def artifact_provenance(self, keys) -> dict[str, tuple[str, str]]:
        """``{artifact key: (source key, how it was learned)}``: what each artifact came from.

        Declared ``source_artifact`` / ``source_image`` features win; run lineage is used only
        when it yields exactly one input. A key absent from the result has no recorded source.
        """
        wanted = sorted({k for k in keys if k})
        if not wanted:
            return {}

        declared = self.artifact_rows(
            wanted, [FEATURES.SOURCE_ARTIFACT, FEATURES.SOURCE_IMAGE])
        out: dict[str, tuple[str, str]] = {}
        for key, row in declared.items():
            source = row.get(FEATURES.SOURCE_ARTIFACT) or row.get(FEATURES.SOURCE_IMAGE)
            if source:
                out[key] = (_key_of(source), "source_artifact")

        unresolved = [k for k in wanted if k not in out]
        if unresolved:
            out.update(self._provenance_from_runs(unresolved))
        return out

    def _provenance_from_runs(self, keys) -> dict[str, tuple[str, str]]:
        """Run lineage, for artifacts that declared nothing. One query, ambiguity refused."""
        import lamindb as ln

        self._mark_read()

        out: dict[str, tuple[str, str]] = {}
        for start in range(0, len(keys), _CHUNK):
            chunk = keys[start:start + _CHUNK]
            try:
                arts = list(ln.Artifact.filter(key__in=chunk).select_related("run")[:len(chunk)])
            except Exception:  # noqa: BLE001 - lineage is optional; its absence is not an error
                return out
            for art in arts:
                run = getattr(art, "run", None)
                if run is None:
                    continue
                try:
                    inputs = [i for i in run.input_artifacts.all() if i.key != art.key]
                except Exception:  # noqa: BLE001
                    continue
                if len(inputs) == 1:
                    out[art.key] = (inputs[0].key, "run_lineage")
        return out

    def artifact_backrefs(self) -> dict[str, dict]:
        """``{artifact key: {roi_uid, roi_name, roi_type}}`` for every role artifact.

        A key absent from the result has no registered Artifact.
        """
        if self._backrefs is not None:
            return self._backrefs

        self._backrefs = self.artifact_rows(
            (key for _rec, _feature, key in self.role_artifacts()),
            [FEATURES.ROI_UID, FEATURES.ROI_NAME, FEATURES.ROI_TYPE],
        )
        return self._backrefs

    def roi_types(self) -> list[str]:
        """Every ROI subtype on the instance, discovered rather than hardcoded."""
        if self._roi_types is not None:
            return self._roi_types

        import lamindb as ln

        self._mark_read()

        self._roi_types = sorted(
            t.name for t in ln.Record.filter(is_type=True, type__name=TYPES.ROI)
        )
        return self._roi_types

    def role_features(self) -> tuple[str, ...]:
        """Feature names whose values are Artifacts playing a role for an ROI.

        Read from installed consumer Vocabularies. Excludes the foundation's provenance features.
        """
        if self._role_features is None:
            from clinicalomx.vocab.registry import discover

            names: set[str] = set()
            for vocab in discover().values():
                names |= {
                    decl.name for decl in vocab.features.values() if _is_artifact_dtype(decl.dtype)
                }
            self._role_features = tuple(sorted(names))
        return self._role_features

    def instance_artifact_features(self) -> tuple[str, ...]:
        """Every Artifact-valued feature the instance declares, whatever is installed here."""
        if self._artifact_features is None:
            self._load_feature_dtypes()
        return self._artifact_features  # type: ignore[return-value]

    def _load_feature_dtypes(self) -> None:
        import lamindb as ln

        self._mark_read()

        names, artifact = set(), set()
        for feature in ln.Feature.filter():
            names.add(feature.name)
            if _is_artifact_dtype(getattr(feature, "_dtype_str", None) or ""):
                artifact.add(feature.name)
        self._feature_names = names
        self._artifact_features = tuple(sorted(artifact))

    def provenance_features(self) -> tuple[str, ...]:
        """The foundation's Artifact-valued provenance features, such as ``source_image``.

        Not part of :meth:`role_features`.
        """
        if self._provenance is None:
            from clinicalomx.vocab.dataio import features

            self._provenance = tuple(sorted(
                row["name"].strip() for row in features()
                if _is_artifact_dtype(row.get("dtype", ""))
                and (row.get("status") or "active").strip() == "active"
            ))
        return self._provenance

    def artifact_keys(self, record, feature: str, *, inherit: bool = False) -> tuple[str, ...]:
        """Artifact keys under ``feature`` on ``record``, whether it is a role or provenance.

        ``inherit`` walks up the ROI spine when the record has none of its own.
        """
        keys = self._own_artifact_keys(record, feature)
        if keys or not inherit:
            return keys
        seen = {getattr(record, "uid", record)}
        parent = self.parent(record)
        while parent is not None and parent.uid not in seen:
            seen.add(parent.uid)
            found = self._own_artifact_keys(parent, feature)
            if found:
                return found
            parent = self.parent(parent)
        return ()

    def _own_artifact_keys(self, record, feature: str) -> tuple[str, ...]:
        keys = self.roles_of(record).get(feature)
        if keys:
            return keys
        value = self.values(record).get(feature)
        if value is None:
            return ()
        many = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        return tuple(_key_of(v) for v in many if v is not None)

    # Indexes over the bulk caches. None of these query.

    def record_by_name(self, type_name: str, name: str):
        """The Record of ``type_name`` called ``name``, or ``None``.

        A duplicate name keeps the first.
        """
        if type_name not in self._by_name:
            index: dict[str, object] = {}
            for rec in self.records(type_name):
                index.setdefault(rec.name, rec)
            self._by_name[type_name] = index
        return self._by_name[type_name].get(name)

    def record_by_uid(self, uid: str):
        """Any ROI Record by uid, from the cache only. ``None`` if it is not an ROI."""
        self._graph()
        return self._by_uid.get(uid)  # type: ignore[union-attr]

    def record_by_any_name(self, name: str):
        """Any ROI Record by name, across every subtype. ``None`` if absent or ambiguous."""
        self._graph()
        hits = [r for r in self._by_uid.values() if str(r.name) == name]  # type: ignore[union-attr]
        return hits[0] if len(hits) == 1 else None

    def roles_of(self, record) -> dict[str, tuple[str, ...]]:
        """``{role feature: (artifact key, ...)}`` for one ROI Record."""
        if self._roles_by_uid is None:
            index: dict[str, dict[str, list[str]]] = {}
            for rec, feature, key in self.role_artifacts():
                index.setdefault(rec.uid, {}).setdefault(feature, []).append(key)
            self._roles_by_uid = {
                uid: {f: tuple(keys) for f, keys in roles.items()}
                for uid, roles in index.items()
            }
        return self._roles_by_uid.get(getattr(record, "uid", record), {})

    def _build_graph(self) -> None:
        """Index the ROI hierarchy from the cached ``parent_roi`` values.

        ``parent_roi`` arrives as the parent's name. Unresolved, ambiguous and multi-parent values
        are recorded in :attr:`_graph_warnings` and skipped.
        """
        by_uid: dict[str, object] = {}
        by_name: dict[str, str] = {}
        ambiguous: set[str] = set()
        for type_name in self.roi_types():
            for rec in self.records(type_name):
                by_uid[rec.uid] = rec
                if by_name.setdefault(rec.name, rec.uid) != rec.uid:
                    ambiguous.add(rec.name)

        warnings = [f"ROI name {n!r} is not unique; parentage through it is ambiguous"
                    for n in sorted(ambiguous)]
        parent_of: dict[str, str] = {}
        children_of: dict[str, list[str]] = {}
        for uid, rec in by_uid.items():
            raw = self.values(rec).get(FEATURES.PARENT_ROI)
            if raw is None:
                continue
            if isinstance(raw, (list, tuple, set, frozenset)):
                warnings.append(
                    f"{rec.name} has {len(raw)} parent_roi values (a merge target); "
                    f"it is not placed in the subtree")
                continue
            name = str(getattr(raw, "name", raw))
            puid = getattr(raw, "uid", None)
            if puid not in by_uid:
                puid = None if name in ambiguous else by_name.get(name)
            if puid is None:
                warnings.append(f"{rec.name}: parent_roi={name!r} resolves to no unique ROI Record")
                continue
            parent_of[uid] = puid
            children_of.setdefault(puid, []).append(uid)

        for kids in children_of.values():
            kids.sort(key=lambda u: by_uid[u].name)
        self._by_uid, self._parent_of, self._children_of = by_uid, parent_of, children_of
        self._graph_warnings = warnings

    def _graph(self) -> dict[str, object]:
        if self._by_uid is None:
            self._build_graph()
        return self._by_uid  # type: ignore[return-value]

    def parent(self, record):
        """The ROI Record ``record`` was split from, or ``None``."""
        self._graph()
        puid = self._parent_of.get(getattr(record, "uid", record))  # type: ignore[union-attr]
        return None if puid is None else self._by_uid[puid]  # type: ignore[index]

    def children(self, record, *, roi_type: str | None = None) -> list:
        """The ROI Records one level below ``record``, name-sorted, optionally of one subtype."""
        self._graph()
        uids = self._children_of.get(getattr(record, "uid", record), [])  # type: ignore[union-attr]
        out = [self._by_uid[u] for u in uids]  # type: ignore[index]
        if roi_type is not None:
            out = [r for r in out if type_name_of(r) == roi_type]
        return out

    def descendants(self, record, *, roi_type: str | None = None) -> list:
        """Every ROI Record below ``record``, breadth-first, optionally of one subtype.

        Cycle-safe.
        """
        self._graph()
        seen = {getattr(record, "uid", record)}
        queue, out = list(self.children(record)), []
        while queue:
            rec = queue.pop(0)
            if rec.uid in seen:
                continue
            seen.add(rec.uid)
            out.append(rec)
            queue.extend(self.children(rec))
        if roi_type is not None:
            out = [r for r in out if type_name_of(r) == roi_type]
        return out

    def graph_warnings(self) -> list[str]:
        """Ambiguities found while indexing the hierarchy."""
        self._graph()
        return list(self._graph_warnings)


def type_name_of(record) -> str | None:
    """A Record's type name, whether ``.type`` is the type Record or already a string."""
    rtype = getattr(record, "type", None)
    return None if rtype is None else str(getattr(rtype, "name", rtype))


def _key_of(value) -> str:
    """The storage key of a role-feature value, whether an Artifact or its key."""
    return getattr(value, "key", None) or str(value)


def _present(value):
    """The value if a record has one, else ``None``.

    NaN, NA and empty collections are absent.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return value or None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass  # an Artifact or Record: a real value
    return value


def _is_artifact_dtype(dtype) -> bool:
    """Is this declared dtype Artifact-valued? Matched by name, without importing lamindb."""
    if isinstance(dtype, str):
        return dtype.replace(" ", "") in {"Artifact", "cat[Artifact]"}
    return getattr(dtype, "__name__", None) == "Artifact"
