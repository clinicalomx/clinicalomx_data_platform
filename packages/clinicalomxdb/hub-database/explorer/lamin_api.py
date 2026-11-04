"""LaminDB introspection and query execution for the Explore tab.

Every entry point closes its database connection when done, and nothing here polls. The UI
must never auto-refresh.
"""

from __future__ import annotations

import functools
import re
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit

import lamindb as ln
from django.db import close_old_connections
from lamindb.models import SQLRecord

# One database operation at a time, for HTTP routes and agent tools alike. Reentrant: these
# functions call one another.
_LOCK = threading.RLock()


def _serialised(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


# Registries that wire the graph together. Hidden from the tree only.
_HIDDEN = {"JsonValue", "Block", "Branch", "Space"}

# Rows per response, and permitted values listed per categorical feature.
MAX_ROWS = 1000
DEFAULT_ROWS = 100
MAX_VALUES = 200

# Lookups offered per field type. Each is passed as a kwarg key, never interpolated into SQL.
_LOOKUPS = {
    "CharField": ["exact", "icontains", "istartswith", "in", "isnull"],
    "TextField": ["exact", "icontains", "istartswith", "isnull"],
    "BooleanField": ["exact", "isnull"],
    "IntegerField": ["exact", "gt", "gte", "lt", "lte", "isnull"],
    "BigIntegerField": ["exact", "gt", "gte", "lt", "lte", "isnull"],
    "SmallIntegerField": ["exact", "gt", "gte", "lt", "lte", "isnull"],
    "FloatField": ["exact", "gt", "gte", "lt", "lte", "isnull"],
    "DateTimeField": ["gt", "gte", "lt", "lte", "date", "isnull"],
    "AutoField": ["exact", "in"],
    "JSONField": ["isnull"],
}
_DEFAULT_LOOKUPS = ["exact", "isnull"]


def _walk_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _walk_subclasses(sub)


def _registries() -> dict[str, Any]:
    """Every concrete registry, keyed by name. Rebuilt per call."""
    out = {}
    for model in _walk_subclasses(SQLRecord):
        meta = getattr(model, "_meta", None)
        if meta is None or meta.abstract or model.__name__ in _HIDDEN:
            continue
        out[model.__name__] = model
    return out


def resolve(name: str):
    """Registry by name from the enumerated set. Never an eval or getattr on user input."""
    model = _registries().get(name)
    if model is None:
        raise LookupError(f"unknown registry {name!r}")
    return model


def _module_of(model) -> str:
    root = model.__module__.split(".")[0]
    return "bionty" if root == "bionty" else "lamindb"


def instance_slug() -> str | None:
    """The connected instance slug, or None. Reads settings, not the database.

    Disconnected lamindb reports the slug "none/none" instead of raising.
    """
    try:
        import lamindb_setup as ln_setup
        slug = ln_setup.settings.instance.slug
        return None if not slug or slug.split("/")[-1] == "none" else slug
    except Exception:
        return None


NO_CONNECTION = {"host": "", "port": "", "database": "", "role": ""}


def connection_fields(dsn: str) -> dict:
    """One connection URL as host, port, database and role. Never reads the password."""
    if not dsn:
        return dict(NO_CONNECTION)
    parts = urlsplit(dsn)
    try:
        port = parts.port
    except ValueError:
        port = None
    return {
        "host": parts.hostname or "",
        "port": str(port) if port else "",
        # Strip one slash: postgres /name gives name, sqlite //path keeps /path.
        "database": parts.path[1:],
        "role": unquote(parts.username or ""),
    }


def instance_connection() -> dict:
    """The connected instance's database endpoint, with no credential. Reads settings only.

    Unknown fields are empty strings. A sqlite instance has only a database, its file path.
    """
    try:
        import lamindb_setup as ln_setup
        dsn = ln_setup.settings.instance.db
    except Exception:
        return dict(NO_CONNECTION)
    return connection_fields(dsn or "")


@_serialised
def list_registries() -> list[dict]:
    try:
        rows = []
        for name, model in sorted(_registries().items()):
            doc = (model.__doc__ or "").strip().split("\n")[0]
            try:
                count = model.objects.count()
            except Exception:
                count = None
            rows.append({
                "name": name,
                "module": _module_of(model),
                "description": doc,
                "count": count,
            })
        return rows
    finally:
        close_old_connections()


# Lookups offered on a feature, by dtype kind. On lamindb 2.8, istartswith, endswith and iexact
# on a feature return zero rows instead of raising, so they are absent.
_FEATURE_LOOKUPS = {
    "str": ["exact", "contains", "icontains", "startswith", "in", "isset", "notset"],
    "num": ["exact", "gt", "gte", "lt", "lte", "isset", "notset"],
    "cat": ["exact", "in", "isset", "notset"],
    "bool": ["exact", "isset", "notset"],
}


def feature_kind(dtype: str) -> str:
    """A feature dtype's kind: cat, num, bool or str. Also used by hub-database/review."""
    if (dtype or "").startswith("cat["):
        return "cat"
    if dtype in ("int", "float", "num"):
        return "num"
    if dtype == "bool":
        return "bool"
    return "str"


def _supports_features(model) -> bool:
    return hasattr(model, "features")


@_serialised
def registry_fields(name: str) -> list[dict]:
    """Filterable fields: model fields, named relations, and every registered Feature.

    Features are not model fields; lamindb accepts them as filter kwargs.
    """
    model = resolve(name)
    fields = []
    for f in model._meta.get_fields():
        if f.name.startswith("_") or f.name.endswith("_id"):
            continue
        if f.is_relation:
            related = getattr(f, "related_model", None)
            if related is None or not hasattr(related, "_meta"):
                continue
            if not any(rf.name == "name" for rf in related._meta.concrete_fields):
                continue
            fields.append({
                "path": f"{f.name}__name",
                "label": f"{f.name} -> name",
                "type": "relation",
                "related": related.__name__,
                "lookups": _LOOKUPS["CharField"],
            })
        else:
            internal = f.get_internal_type()
            fields.append({
                "path": f.name,
                "label": f.name,
                "type": internal,
                "related": None,
                "lookups": _LOOKUPS.get(internal, _DEFAULT_LOOKUPS),
            })

    fields.sort(key=lambda d: d["path"])
    if not _supports_features(model):
        return fields

    # A feature named like a model field gets no lookups: lamindb resolves the kwarg to the column.
    taken = {f["path"] for f in fields} | {f.name for f in model._meta.get_fields()}
    for feat in list_features():
        kind = feature_kind(feat["dtype"])
        entry = {
            "path": feat["name"],
            "label": f"feature: {feat['name']}",
            "type": "feature",
            "dtype": feat["dtype"],
            "related": None,
            "values": feat["values"],
            "lookups": _FEATURE_LOOKUPS[kind],
        }
        if feat["name"] in taken:
            entry["shadowed"] = True
            entry["label"] = f"feature: {feat['name']} (shadowed by a field of the same name)"
            entry["lookups"] = []
        fields.append(entry)
    return fields


_CAT = re.compile(r"cat\[(.+)\]$")


def _feature_values(dtype: str) -> tuple[list[str] | None, bool]:
    """Permitted values for a categorical feature, and whether the list is incomplete.

    dtype looks like cat[ULabel], cat[bionty.CellType] or cat[ULabel[xnf00gBl]]; the inner
    bracket holds the type record's UID, not its name. Values are None for a non-categorical
    dtype. Incomplete means a part hit MAX_VALUES or could not be resolved or queried.
    """
    m = _CAT.match(dtype or "")
    if not m:
        return None, False
    values: list[str] = []
    incomplete = False
    for part in m.group(1).split("|"):
        part = part.strip()
        type_uid = None
        if "[" in part and part.endswith("]"):
            part, type_uid = part[:-1].split("[", 1)
        registry = part.split(".")[-1]
        try:
            model = resolve(registry)
        except LookupError:
            incomplete = True
            continue
        qs = model.objects.filter(type__uid=type_uid) if type_uid else model.objects.all()
        try:
            page = list(qs.values_list("name", flat=True)[:MAX_VALUES])
        except Exception:
            incomplete = True
            continue
        # Counted before blank names are dropped.
        incomplete = incomplete or len(page) >= MAX_VALUES
        values += [v for v in page if v]
    unique = sorted(set(values))
    return unique[:MAX_VALUES], incomplete or len(unique) > MAX_VALUES


_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 20.0


def _cached(key: str, fn):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = fn()
    _CACHE[key] = (now, value)
    return value


@_serialised
def list_features() -> list[dict]:
    return _cached("features", _list_features_uncached)


def _list_features_uncached() -> list[dict]:
    """The Feature registry, with permitted values where the dtype names a registry."""
    try:
        rows = []
        for f in ln.Feature.objects.all().order_by("name"):
            dtype = getattr(f, "dtype_as_str", None) or getattr(f, "_dtype_str", "") or ""
            values, incomplete = _feature_values(dtype)
            rows.append({
                "name": f.name,
                "dtype": dtype,
                "description": f.description,
                "unit": f.unit,
                "values": values,
                "values_incomplete": incomplete,
            })
        return rows
    finally:
        close_old_connections()


@_serialised
def list_entity_types() -> list[dict]:
    """Record types (is_type=True) with instance counts: Cohort, Patient, Sample, Slide, ROI."""
    try:
        rows = []
        for t in ln.Record.objects.filter(is_type=True).order_by("name"):
            rows.append({
                "name": t.name,
                "uid": t.uid,
                "description": t.description,
                "count": ln.Record.objects.filter(type=t).count(),
            })
        return rows
    finally:
        close_old_connections()


def scalar(value: str, spec: dict):
    """Convert one form value to the type the target expects. Also used by hub-database/review.

    Numeric features need a real int or float: on lamindb 2.8 the string "4" matches zero rows.
    """
    if spec.get("type") == "feature":
        kind = feature_kind(spec.get("dtype", ""))
        if kind == "num":
            try:
                return int(value)
            except ValueError:
                return float(value)
        if kind == "bool":
            return str(value).lower() in ("1", "true", "yes")
    return value


def _coerce(value: str, lookup: str, spec: dict):
    """Form values arrive as strings; a few lookups and dtypes need something else."""
    if lookup == "isnull":
        return str(value).lower() in ("1", "true", "yes")
    if lookup == "in":
        # A list from the page, or a comma-separated string from a hand-written URL.
        items = value if isinstance(value, (list, tuple)) else str(value).split(",")
        return [scalar(str(v).strip(), spec) for v in items if str(v).strip()]
    return scalar(value, spec)


def filter_terms(registry: str, filters: list[dict]) -> list[tuple[str, Any, bool]]:
    """Builder filters as an ordered list of (kwarg, value, is_feature) terms.

    Each term goes in its own chained filter(): lamindb 2.8 raises InvalidArgument when one
    filter() mixes a feature kwarg and a column kwarg.
    """
    specs = {f["path"]: f for f in registry_fields(registry)}
    terms: list[tuple[str, Any, bool]] = []
    for f in filters:
        path = f["field"]
        spec = specs.get(path)
        if spec is None:
            raise ValueError(f"{path!r} is not a filterable field on {registry}")
        lookup = f.get("lookup", "exact")
        if lookup not in spec["lookups"]:
            reason = ("it is shadowed by a model field of the same name"
                      if spec.get("shadowed") else f"allowed: {', '.join(spec['lookups'])}")
            raise ValueError(f"{lookup!r} is not available on {path!r} ({reason})")
        # isset/notset ask whether a row carries the field at all. Any form value is ignored.
        is_feature = spec["type"] == "feature"
        if lookup in ("isset", "notset"):
            terms.append((f"{path}__isnull", lookup == "notset", is_feature))
            continue
        key = path if lookup == "exact" else f"{path}__{lookup}"
        operand = _coerce(f["value"], lookup, spec)
        if lookup == "in" and not operand:
            raise ValueError(f"{path!r} uses 'in' but no values were given")
        terms.append((key, operand, is_feature))
    return terms


# An OR over feature terms unions ids into one IN clause. Refused beyond this many ids.
MAX_OR_IDS = 20000


def normalise_groups(filters: list[dict] | None, groups: list[dict] | None,
                     match: str) -> tuple[list[dict], str]:
    """Return (groups, outer_match). A flat filter list becomes one group with that match mode."""
    if groups:
        return [g for g in groups if g.get("filters")], match
    if filters:
        return [{"match": match, "filters": filters}], "all"
    return [], "all"


def _term_ids(model, key, value) -> set:
    return set(model.filter(**{key: value}).values_list("id", flat=True))


def _guard(ids: set) -> set:
    if len(ids) > MAX_OR_IDS:
        raise ValueError(
            f"this OR matches more than {MAX_OR_IDS} rows. Feature terms cannot be OR-ed in the "
            "database, so the ids are unioned here, and beyond this the query stops being "
            "sensible. Narrow it with an AND condition first, or use the console.")
    return ids


def _group_ids(model, registry: str, terms, inner: str) -> tuple[set, str]:
    """Every id matching one group, and the expression describing how it was found.

    AND chains filters. OR over columns uses Q. OR involving a feature unions per-term ids,
    because Q raises FieldError on a feature name.
    """
    base = f"ln.{registry}"
    if inner != "any" or len(terms) < 2:
        qs = model.filter()
        for key, value, _ in terms:
            qs = qs.filter(**{key: value})
        rendered = "".join(f".filter({k}={v!r})" for k, v, _ in terms) or ".filter()"
        return set(qs.values_list("id", flat=True)), base + rendered

    if not any(is_feature for _, _, is_feature in terms):
        from django.db.models import Q
        clause = Q()
        for key, value, _ in terms:
            clause |= Q(**{key: value})
        rendered = " | ".join(f"Q({k}={v!r})" for k, v, _ in terms)
        return (set(model.filter(clause).values_list("id", flat=True)),
                f"{base}.filter({rendered})")

    ids: set = set()
    for key, value, _ in terms:
        ids = _guard(ids | _term_ids(model, key, value))
    parts = " ".join(f"*{base}.filter({k}={v!r}).values_list('id', flat=True)," for k, v, _ in terms)
    return ids, f"{base}.filter(id__in={{{parts}}})"


def build_queryset(model, registry: str, groups: list[dict], outer: str):
    """The queryset for these groups, and the expression describing it.

    outer joins the groups; each group's match joins its conditions.
    """
    if outer not in ("all", "any"):
        raise ValueError(f"unknown match mode {outer!r} (allowed: all, any)")
    for g in groups:
        if g.get("match", "all") not in ("all", "any"):
            raise ValueError(f"unknown group match mode {g.get('match')!r} (allowed: all, any)")

    base = f"ln.{registry}"
    if not groups:
        return model.filter(), f"{base}.filter()"

    prepared = [(filter_terms(registry, g.get("filters", [])), g.get("match", "all"))
                for g in groups]

    if len(prepared) == 1 and prepared[0][1] != "any":
        terms = prepared[0][0]
        qs = model.filter()
        for key, value, _ in terms:
            qs = qs.filter(**{key: value})
        return qs, base + ("".join(f".filter({k}={v!r})" for k, v, _ in terms) or ".filter()")

    if outer == "all":
        qs = model.filter()
        rendered = base
        for terms, inner in prepared:
            if inner != "any" or len(terms) < 2:
                for key, value, _ in terms:
                    qs = qs.filter(**{key: value})
                rendered += "".join(f".filter({k}={v!r})" for k, v, _ in terms)
            else:
                ids, expr = _group_ids(model, registry, terms, inner)
                qs = qs.filter(id__in=ids)
                rendered += f".filter(id__in={expr}.values_list('id', flat=True))"
        return qs, rendered

    union: set = set()
    parts = []
    for terms, inner in prepared:
        ids, expr = _group_ids(model, registry, terms, inner)
        union = _guard(union | ids)
        parts.append(f"*{expr}.values_list('id', flat=True),")
    return model.filter(id__in=union), f"{base}.filter(id__in={{{' '.join(parts)}}})"


def build_expression(registry: str, features: bool, limit: int, base: str,
                     merge: list[str] | None = None) -> str:
    """The equivalent lamindb call, for display only. Never evaluated."""
    chain = base
    if merge:
        tail = f'to_dataframe()  # merged features: {", ".join(merge)}'
    else:
        tail = 'to_dataframe(include="features")' if features else "to_dataframe()"
    return f"{chain}.order_by('-created_at')[:{limit}].{tail}"


# Image cells longer than this are withheld.
MAX_IMAGE_CHARS = 1_000_000

# Formats a browser draws. TIFF is excluded.
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _image_mime(value: str) -> str | None:
    """The MIME type of a base64 image a browser can draw, "data-uri" for one, else None.

    Decided by content, not by column name.
    """
    if not isinstance(value, str) or len(value) < 32:
        return None
    if value.startswith("data:image/"):
        return "data-uri"
    head = value[:32]
    if not all(c.isalnum() or c in "+/=" for c in head):
        return None
    import base64
    import binascii
    try:
        raw = base64.b64decode(head + "==" * 2, validate=False)
    except (binascii.Error, ValueError):
        return None
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            return mime
    # WEBP is RIFF....WEBP, so the marker sits past the four length bytes.
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_columns(columns: list[str], rows: list[list[str]]) -> list[str]:
    """Columns whose first non-empty value is an image."""
    found = []
    for index, name in enumerate(columns):
        for row in rows:
            if not row[index]:
                continue
            if _image_mime(row[index]):
                found.append(name)
            break
    return found


def _frame_to_payload(df, elapsed: float, expression: str, limit: int) -> dict:
    columns = [str(c) for c in df.columns]
    rows = []
    for record in df.to_dict("records"):
        rows.append([_render(record[c]) for c in df.columns])
    images = _image_columns(columns, rows)
    oversized = 0
    if images:
        indices = [i for i, name in enumerate(columns) if name in images]
        for row in rows:
            for index in indices:
                value = row[index]
                if not value:
                    continue
                if len(value) > MAX_IMAGE_CHARS:
                    row[index] = ""
                    oversized += 1
                    continue
                # Each cell is resolved on its own and rewritten as a complete data URI.
                mime = _image_mime(value)
                if mime == "data-uri":
                    continue
                if mime:
                    row[index] = f"data:{mime};base64,{value}"
    return {
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "truncated": len(rows) >= limit,
        "elapsed": round(elapsed, 3),
        "expression": expression,
        "image_columns": images,
        "images_omitted": oversized if images else 0,
    }


def _render(value) -> str:
    """Cell text. A related object renders as its name or uid, never its full repr."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_render(v) for v in value)
    # Any Django model: Branch, Space and User do not descend from SQLRecord.
    if hasattr(value, "_meta") and hasattr(value, "pk"):
        return getattr(value, "name", None) or getattr(value, "uid", "") or str(value)
    return str(value)


def _resolve_merge(registry: str, features: bool, feature_columns: list[str] | None) -> list[str]:
    """The selected feature columns, checked against the registry. [] when none are selected."""
    if feature_columns:
        known = {f["path"] for f in registry_fields(registry) if f["type"] == "feature"}
        unknown = [c for c in feature_columns if c not in known]
        if unknown:
            raise ValueError(f"not features on {registry}: {', '.join(unknown)}")
        return list(feature_columns)
    return []


@_serialised
def run_query(registry: str, filters: list[dict] | None = None, features: bool = False,
              limit: int = DEFAULT_ROWS, match: str = "all",
              feature_columns: list[str] | None = None,
              groups: list[dict] | None = None) -> dict:
    """The builder's query. Filters become kwargs and the registry is resolved by name; no eval.

    Unknown filter paths are rejected.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    model = resolve(registry)
    grouped, outer = normalise_groups(filters, groups, match)
    try:
        started = time.monotonic()
        # model.filter, not model.objects.filter: only lamindb's filter resolves feature kwargs.
        qs, base = build_queryset(model, registry, grouped, outer)
        # to_dataframe defaults to limit=20 and ignores a queryset slice, so both are explicit.
        df = qs.to_dataframe(limit=limit, order_by="-created_at")

        # Feature columns merge onto the displayed page only.
        merge = _resolve_merge(registry, features, feature_columns)
        if features and not merge:
            in_scope = features_in_scope(registry, filters, match, groups) or []
            merge = [f["name"] for f in in_scope]
        expression = build_expression(registry, features, limit, base,
                                      merge if feature_columns else None)
        df = _apply_merge(df, model, registry, merge)

        payload = _frame_to_payload(df, time.monotonic() - started, expression, limit)
        payload["merged"] = merge
        # Features carried by the unsliced result set.
        payload["features_in_scope"] = features_in_scope(registry, filters, match, groups)
        return payload
    finally:
        close_old_connections()


def _scope_queryset(model, registry: str, groups, outer: str = "all"):
    """The unsliced queryset for these groups."""
    return build_queryset(model, registry, groups, outer)[0]


def _fv_relation(model) -> str | None:
    """The FeatureValue field pointing back at this registry, or None."""
    from lamindb.models import FeatureValue
    for f in FeatureValue._meta.get_fields():
        if f.is_relation and getattr(f, "related_model", None) is model:
            return f.name
    return None


@_serialised
def features_in_scope(registry: str, filters: list[dict] | None = None, match: str = "all",
                      groups: list[dict] | None = None) -> list[dict] | None:
    """Features present on the rows matching these filters, with their observed values.

    Plain values come from FeatureValue rows, categorical values from ulabel links. Returns None
    when the registry cannot carry features.
    """
    model = resolve(registry)
    if not _supports_features(model):
        return None
    try:
        grouped, outer = normalise_groups(filters, groups, match)
        qs = _scope_queryset(model, registry, grouped, outer)
        found: dict[str, set] = {}

        relation = _fv_relation(model)
        if relation:
            from lamindb.models import FeatureValue
            pairs = FeatureValue.objects.filter(**{f"{relation}__in": qs}) \
                                        .values_list("feature__name", "value")
            for name, value in pairs:
                if name:
                    found.setdefault(name, set()).add(_render(value))

        if any(f.name == "links_ulabel" for f in model._meta.get_fields()):
            pairs = qs.values_list("links_ulabel__feature__name", "links_ulabel__ulabel__name")
            for name, value in pairs:
                if name:
                    found.setdefault(name, set()).add(_render(value))

        dtypes = {f["name"]: f["dtype"] for f in list_features()}
        return [
            {
                "name": name,
                "dtype": dtypes.get(name, ""),
                "values": sorted(v for v in values if v)[:MAX_VALUES],
            }
            for name, values in sorted(found.items())
        ]
    finally:
        close_old_connections()


def _apply_merge(df, model, registry: str, merge: list[str]):
    """Add the merged feature columns to a frame, right after the identifying columns."""
    if not merge or "uid" not in df.columns:
        return df
    uids = list(df["uid"])
    values = merge_feature_columns(model, registry, model.filter(uid__in=uids), merge)
    for name in merge:
        column = values.get(name, {})
        df[name] = [column.get(u, "") for u in uids]
    identity = [c for c in ("uid", "key", "name", "description") if c in df.columns
                and c not in merge]
    rest = [c for c in df.columns if c not in identity and c not in merge]
    return df[identity + [c for c in merge if c in df.columns] + rest]


def merge_feature_columns(model, registry: str, qs, names: list[str]) -> dict[str, dict[str, str]]:
    """Per-row values for the named features, as {feature: {uid: text}}.

    Several values for one row are joined with commas.
    """
    wanted = set(names)
    if not wanted:
        return {}
    columns: dict[str, dict[str, list]] = {n: {} for n in names}
    id_to_uid = dict(qs.values_list("id", "uid"))

    relation = _fv_relation(model)
    if relation:
        from lamindb.models import FeatureValue
        rows = (FeatureValue.objects
                .filter(**{f"{relation}__in": qs}, feature__name__in=wanted)
                .values_list("feature__name", f"{relation}__id", "value"))
        for name, row_id, value in rows:
            uid = id_to_uid.get(row_id)
            if uid is not None and value is not None:
                columns[name].setdefault(uid, []).append(value)

    if any(f.name == "links_ulabel" for f in model._meta.get_fields()):
        rows = qs.values_list("id", "links_ulabel__feature__name", "links_ulabel__ulabel__name")
        for row_id, name, value in rows:
            if name in wanted and value is not None:
                uid = id_to_uid.get(row_id)
                if uid is not None:
                    columns[name].setdefault(uid, []).append(value)

    return {name: {uid: _render(vals[0]) if len(vals) == 1 else ", ".join(_render(v) for v in vals)
                   for uid, vals in per_uid.items()}
            for name, per_uid in columns.items()}


AGGREGATES = ["count", "describe", "unique", "nunique", "min", "max", "sum", "mean"]
_NUMERIC_OPS = {"min", "max", "sum", "mean"}
_NUMERIC_COLUMNS = {"IntegerField", "BigIntegerField", "SmallIntegerField", "FloatField",
                    "DecimalField", "PositiveIntegerField", "PositiveBigIntegerField"}
_TOP_N = 5


def _is_numeric(spec: dict) -> bool:
    if spec["type"] == "feature":
        return feature_kind(spec.get("dtype", "")) == "num"
    return spec["type"] in _NUMERIC_COLUMNS


def _describe(qs, model, registry: str, spec: dict, target: str, base: str, started: float) -> dict:
    """A pandas-style summary of one field over the whole result set.

    Numbers get distribution statistics; other fields get their most common values. "missing"
    counts rows without a value.
    """
    total = qs.count()
    if spec["type"] == "feature":
        values = [v for v in _feature_values_in_scope(model, qs, target) if v is not None]
        expr = (f'FeatureValue.objects.filter({registry.lower()}s__in={base}, '
                f'feature__name={target!r})  # summarised in Python: features are not columns')
    else:
        values = [v for v in qs.values_list(target, flat=True) if v is not None]
        expr = f'{base}.values_list({target!r}, flat=True)  # summarised in Python'

    stats: list[tuple[str, Any]] = [
        ("rows in result", total),
        ("present", len(values)),
        ("missing", total - len(values)),
        ("distinct", len({_render(v) for v in values})),
    ]

    if _is_numeric(spec):
        numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numbers:
            ordered = sorted(numbers)
            n = len(ordered)
            mean = sum(ordered) / n
            # Population standard deviation.
            std = (sum((x - mean) ** 2 for x in ordered) / n) ** 0.5
            quantile = lambda q: ordered[min(n - 1, int(q * (n - 1) + 0.5))]
            stats += [("min", ordered[0]), ("25%", quantile(0.25)), ("median", quantile(0.5)),
                      ("75%", quantile(0.75)), ("max", ordered[-1]),
                      ("mean", round(mean, 6)), ("std", round(std, 6)), ("sum", sum(ordered))]
        else:
            stats.append(("note", "no numeric values on this result set"))
    else:
        counts: dict[str, int] = {}
        for v in values:
            key = _render(v)
            counts[key] = counts.get(key, 0) + 1
        for i, (value, n) in enumerate(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_N], 1):
            stats.append((f"most common {i}", f"{value or '(empty)'}  ({n})"))
        if len(counts) > _TOP_N:
            stats.append(("...", f"{len(counts) - _TOP_N} further distinct values"))

    return _agg_payload(["statistic", target], [[k, v] for k, v in stats], expr, started)


def _feature_values_in_scope(model, qs, target: str) -> list:
    """Every value of one feature across the result set, typed as stored."""
    values: list = []
    relation = _fv_relation(model)
    if relation:
        from lamindb.models import FeatureValue
        values += list(FeatureValue.objects
                       .filter(**{f"{relation}__in": qs}, feature__name=target)
                       .values_list("value", flat=True))
    if any(f.name == "links_ulabel" for f in model._meta.get_fields()):
        # lamindb's filter rejects links_ulabel__ paths; values_list accepts them.
        values += [v for name, v in
                   qs.values_list("links_ulabel__feature__name", "links_ulabel__ulabel__name")
                   if name == target and v is not None]
    return values


@_serialised
def aggregate(registry: str, filters: list[dict] | None, target: str | None, op: str,
              match: str = "all", groups: list[dict] | None = None) -> dict:
    """Summarise the whole result set. The row limit does not apply.

    Columns aggregate in the database; feature values are reduced in Python.
    """
    if op not in AGGREGATES:
        raise ValueError(f"unknown operation {op!r} (allowed: {', '.join(AGGREGATES)})")
    model = resolve(registry)
    grouped, outer = normalise_groups(filters, groups, match)

    try:
        qs, base = build_queryset(model, registry, grouped, outer)
        started = time.monotonic()

        if op == "count":
            return _agg_payload(["count"], [[qs.count()]], f"{base}.count()", started)

        if not target:
            raise ValueError(f"{op!r} needs a target field")
        spec = {f["path"]: f for f in registry_fields(registry)}.get(target)
        if spec is None:
            raise ValueError(f"{target!r} is not a field on {registry}")

        if op == "describe":
            return _describe(qs, model, registry, spec, target, base, started)

        if spec["type"] == "feature":
            values = _feature_values_in_scope(model, qs, target)
            expr = (f'FeatureValue.objects.filter({registry.lower()}s__in={base}, '
                    f'feature__name={target!r}).values_list("value", flat=True)  '
                    f'# reduced in Python: features are not columns')
        else:
            values = None
            expr = None

        if op in ("unique", "nunique"):
            if values is None:
                distinct = sorted({_render(v) for v in
                                   qs.values_list(target, flat=True).distinct() if v is not None})
                expr = f'{base}.values_list({target!r}, flat=True).distinct()'
            else:
                distinct = sorted({_render(v) for v in values if v is not None})
            if op == "nunique":
                return _agg_payload(["nunique"], [[len(distinct)]], expr, started)
            capped = distinct[:MAX_ROWS]
            return _agg_payload([target], [[v] for v in capped], expr, started,
                                truncated=len(distinct) > len(capped))

        # min / max / sum / mean
        if values is None:
            from django.db.models import Avg, Max, Min, Sum
            fn = {"min": Min, "max": Max, "sum": Sum, "mean": Avg}[op]
            result = qs.aggregate(v=fn(target))["v"]
            expr = f'{base}.aggregate({fn.__name__}({target!r}))'
        else:
            numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if not numbers:
                raise ValueError(f"{target!r} holds no numeric values on this result set")
            result = {"min": min, "max": max, "sum": sum,
                      "mean": lambda n: sum(n) / len(n)}[op](numbers)
        return _agg_payload([op], [[_render(result)]], expr, started)
    finally:
        close_old_connections()


def _agg_payload(columns, rows, expression, started, truncated=False) -> dict:
    return {
        "columns": columns,
        "rows": [[_render(c) for c in row] for row in rows],
        "count": len(rows),
        "truncated": truncated,
        "elapsed": round(time.monotonic() - started, 3),
        "expression": expression,
    }


EXPORT_MAX_ROWS = 50_000


@_serialised
def export_csv(registry: str, filters: list[dict] | None = None, features: bool = False,
               match: str = "all", columns: list[str] | None = None,
               feature_columns: list[str] | None = None,
               groups: list[dict] | None = None) -> tuple[str, int, bool, list[str]]:
    """The whole result set as CSV, with row count, whether it hit the cap, and dropped columns.

    Dropped columns are requested columns the frame does not have.
    """
    import csv
    import io

    model = resolve(registry)
    grouped, outer = normalise_groups(filters, groups, match)
    try:
        qs, _ = build_queryset(model, registry, grouped, outer)
        # Explicit limit, as in run_query. One over the cap detects truncation.
        df = qs.to_dataframe(limit=EXPORT_MAX_ROWS + 1, order_by="-created_at")

        truncated = len(df) > EXPORT_MAX_ROWS
        if truncated:
            df = df.head(EXPORT_MAX_ROWS)

        merge = _resolve_merge(registry, features, feature_columns)
        if features and not merge:
            merge = [f["name"] for f in
                     (features_in_scope(registry, filters, match, groups) or [])]
        df = _apply_merge(df, model, registry, merge)

        available = [str(c) for c in df.columns]
        chosen = [c for c in (columns or available) if c in available] or available
        dropped = [c for c in (columns or []) if c not in available]

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(chosen)
        for record in df.to_dict("records"):
            writer.writerow([_render(record[c]) for c in chosen])
        return buffer.getvalue(), len(df), truncated, dropped
    finally:
        close_old_connections()


@_serialised
def record_detail(registry: str, uid: str) -> dict:
    """Fields and related object names for one row. Each relation is capped at 50 names."""
    model = resolve(registry)
    try:
        obj = model.objects.get(uid=uid)
        fields = {}
        for f in model._meta.concrete_fields:
            if f.name.startswith("_"):
                continue
            fields[f.name] = _render(getattr(obj, f.name, None))
        relations = {}
        for f in model._meta.get_fields():
            if not f.is_relation or not f.auto_created or f.concrete:
                continue
            try:
                manager = getattr(obj, f.get_accessor_name(), None)
                if manager is None:
                    continue
                names = list(manager.values_list("name", flat=True)[:50])
                if names:
                    relations[f.get_accessor_name()] = names
            except Exception:
                continue
        return {"registry": registry, "uid": uid, "fields": fields, "relations": relations}
    finally:
        close_old_connections()
