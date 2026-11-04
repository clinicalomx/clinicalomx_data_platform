"""InputRef validation and model-resolver injection. No database."""
import pytest
from pydantic import ValidationError

from clinicalomx import InputRef, register_model_resolver
from clinicalomx.refs import _REGISTRY_ATTR


@pytest.mark.parametrize("kw", [
    {"kind": "artifact", "key": "a/b"},
    {"kind": "artifact", "uid": "X0000"},
    {"kind": "artifact", "key": "a/b", "version": "1"},
    {"kind": "artifact", "key": "a/b", "source": "b.json"},
    {"kind": "collection", "key": "c/d"},
    {"kind": "record", "uid": "R0000"},
    {"kind": "record", "query": {"name": "TMA", "is_type": True}},
    {"kind": "ulabel", "query": {"name": "DAPI"}},
    {"kind": "transform", "key": "t/x"},
    {"kind": "model", "alias": "prod", "objective": "general"},
    {"kind": "model", "version": "3", "objective": "core"},
    {"kind": "model", "uid": "M0000"},
])
def test_valid(kw):
    InputRef(**kw)


@pytest.mark.parametrize("kw", [
    {"kind": "artifact"},                              # no selector
    {"kind": "artifact", "uid": "x", "key": "y"},           # two selectors
    {"kind": "record", "uid": "x", "version": "2"},         # version w/o key
    {"kind": "artifact", "key": "y", "alias": "p"},         # model field on registry kind
    {"kind": "model", "alias": "a", "version": "3"},        # two model coords
    {"kind": "model", "alias": "a", "query": {"name": "z"}}, # query on model
    {"kind": "banana", "uid": "x"},                       # bad kind
])
def test_invalid(kw):
    # ValidationError, or the model's own ValueError for a bad `kind`.
    with pytest.raises((ValidationError, ValueError)):
        InputRef(**kw)


def test_model_resolver_injection():
    register_model_resolver(lambda **kw: ("RESOLVED", kw))
    out = InputRef(kind="model", alias="prod", objective="general").get()
    assert out == ("RESOLVED", {"alias": "prod", "objective": "general"})


def test_model_kind_requires_resolver(monkeypatch):
    import clinicalomx.refs as refs
    monkeypatch.setattr(refs, "_MODEL_RESOLVER", None)
    with pytest.raises(RuntimeError, match="register_model_resolver"):
        InputRef(kind="model", alias="prod").get()


def test_every_registry_kind_maps():
    for kind in _REGISTRY_ATTR:
        InputRef(kind=kind, uid="X0000")  # uid is universal
