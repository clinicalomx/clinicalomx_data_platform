"""Create one CDM release's record types, features and schemas on the connected instance.

    from clinicalomx_omop import declare
    types = declare.ensure()          # {table name: the ln.Record type its rows go under}

Per table, all named `OMOP_CDM_v<version>.<table>` (`Release.type_name`):

- an `ln.Feature` type holding one `ln.Feature` per CDM field;
- an `ln.Schema` over those features, with `minimal_set=True` (CDM-required fields are
  required) and `maximal_set=True` (fields the CDM does not define are refused);
- an `ln.Record` type with that schema, so every row is validated on write.

A new release is a second set of types beside the first. `ensure` is idempotent and refuses
a type that exists with another shape.
"""

from __future__ import annotations

from .cdm import CURRENT, Field, Release

#: Parent types in the platform's type tree. `person` rows are the platform's `Patient`, so a
#: core ROI's `patient` feature links to them. Other tables group under the release type.
PLATFORM_PARENTS = {"person": "Patient"}


class ShapeConflict(RuntimeError):
    """A name this release declares already exists on the instance with another shape."""


def _record_type(name: str, *, parent=None, schema=None, description: str = ""):
    import lamindb as ln

    found = ln.Record.filter(name=name, is_type=True).one_or_none()
    if found is None:
        return ln.Record(name=name, is_type=True, type=parent, schema=schema,
                         description=description or None).save()
    if schema is not None and found.schema_id != schema.id:
        raise ShapeConflict(f"record type {name!r} exists bound to another schema")
    if parent is not None and found.type_id != parent.id:
        raise ShapeConflict(f"record type {name!r} exists under another parent type")
    return found


def _feature(field: Field, feature_type):
    import lamindb as ln
    from lamindb.models.feature import serialize_dtype

    found = ln.Feature.filter(name=field.name, type=feature_type).one_or_none()
    if found is None:
        return ln.Feature(name=field.name, dtype=field.dtype, type=feature_type,
                          description=field.description or None).save()
    expected = serialize_dtype(field.dtype)
    if found._dtype_str != expected:
        raise ShapeConflict(f"feature {feature_type.name}/{field.name} exists as "
                            f"{found._dtype_str}, the CDM says {expected}")
    return found


def _type_of(model, name: str):
    found = model.filter(name=name, is_type=True).one_or_none()
    return found or model(name=name, is_type=True).save()


def ensure(release: Release = CURRENT) -> dict:
    """Declare `release` on the connected instance. Returns {table name: record type}.

    Needs the foundation's `Patient` type (`python -m clinicalomx.vocab apply`).
    """
    import lamindb as ln

    parents = {}
    for table, type_name in PLATFORM_PARENTS.items():
        parents[table] = ln.Record.filter(name=type_name, is_type=True).one_or_none()
        if parents[table] is None:
            raise LookupError(f"the {type_name!r} record type is missing; seed the foundation "
                              "vocabulary first: python -m clinicalomx.vocab apply")

    group = _record_type(release.name, description=(
        f"OMOP CDM v{release.version}, {release.tag} ({release.commit})."))
    schema_type = _type_of(ln.Schema, release.name)
    declared = {}
    for table in release.tables:
        name = release.type_name(table.name)
        feature_type = _type_of(ln.Feature, name)
        features = [_feature(f, feature_type) for f in table.fields]
        members = [feat if f.required else feat.with_config(optional=True)
                   for f, feat in zip(table.fields, features, strict=True)]
        schema = ln.Schema.filter(name=name, is_type=False).one_or_none()
        if schema is None:
            schema = ln.Schema(name=name, type=schema_type, features=members,
                               minimal_set=True, maximal_set=True,
                               description=table.description).save()
        elif {f.name for f in schema.members} != {f.name for f in table.fields}:
            raise ShapeConflict(f"schema {name!r} exists over other features")
        declared[table.name] = _record_type(
            name, parent=parents.get(table.name, group), schema=schema,
            description=table.description)
    return declared
