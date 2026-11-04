"""Pure-logic tests for clinicalomx.roi. No database."""
import pytest

import clinicalomx
from clinicalomx import ROI_UID


class _Features:
    def __init__(self, vals=None, raises=False):
        self._vals, self._raises = vals or {}, raises

    def get_values(self):
        if self._raises:
            raise RuntimeError("no features backend")
        return self._vals


class _Artifact:
    key = "demo/x.zarr"

    def __init__(self, vals=None, raises=False):
        self.features = _Features(vals, raises)


def test_constants():
    assert ROI_UID == "roi_uid"


def test_resolve_none_when_no_backref():
    # Empty features: no roi_uid and no legacy feature, so None without touching lamindb.
    assert clinicalomx.resolve_roi(_Artifact(vals={})) is None
    # A features backend that errors gives None.
    assert clinicalomx.resolve_roi(_Artifact(raises=True)) is None


def test_require_roi_raises_when_unanchored():
    with pytest.raises(ValueError, match="annotates no ROI"):
        clinicalomx.require_roi(_Artifact(vals={}))


def test_update_returns_none_when_input_unanchored_non_strict():
    # No ROI on the input and strict=False: None, before any lamindb access.
    assert clinicalomx.update(_Artifact(vals={}), _Artifact(vals={}), "segmentations") is None


def test_update_strict_raises():
    with pytest.raises(ValueError, match="no ROI to inherit"):
        clinicalomx.update(_Artifact(vals={}), _Artifact(vals={}), "segmentations", strict=True)


# Annotate's idempotency guard.
# `_already_linked` matches both shapes get_values() returns: objects, and cat[] key strings.
from clinicalomx.roi import _already_linked  # noqa: E402


class _ROI:
    def __init__(self, vals):
        self.features = _Features(vals)


class _Art:
    def __init__(self, uid="art-uid", key="demo/x.parquet"):
        self.uid, self.key = uid, key


ROLE = "segmentations"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, False),
        ("demo/x.parquet", True),                       # key string
        ("art-uid", True),                              # uid string
        ("demo/other.parquet", False),
        ({"demo/other.parquet", "demo/x.parquet"}, True),   # multi-valued set of key strings
        ({"demo/other.parquet"}, False),
        ([_Art()], True),                               # object form
        ([_Art(uid="z", key="demo/z.parquet")], False),
        ([None], False),                                # a null never matches
    ],
    ids=["absent", "key-str", "uid-str", "other-key", "set-hit", "set-miss",
         "object-hit", "object-miss", "null-value"],
)
def test_already_linked_across_every_stored_shape(stored, expected):
    roi = _ROI({} if stored is None else {ROLE: stored})
    assert _already_linked(roi, _Art(), ROLE) is expected


def test_already_linked_is_scoped_to_the_feature():
    roi = _ROI({"other_role": "demo/x.parquet"})
    assert _already_linked(roi, _Art(), ROLE) is False


def test_an_artifact_without_uid_or_key_never_false_positives():
    class _Bare:
        uid = key = None

    assert _already_linked(_ROI({ROLE: [None]}), _Bare(), ROLE) is False