"""The pipeline's front-door queries over the instance."""
from __future__ import annotations


def source_artifacts(suffix: str = ".qptiff", *, is_latest: bool = True):
    """Every source artifact whose key ends with ``suffix``.

    Matches the key, never the ``suffix`` field. lamindb stores ``.qptiff`` as ``""`` there on newer
    rows, so a ``suffix`` filter skips them.
    """
    import lamindb as ln

    return ln.Artifact.filter(key__endswith=suffix, is_latest=is_latest)

def select_registered_slide_uids(limit: int | None = None) -> list[str]:
    """WholeSlide Record uids in the ``registered`` state, waiting for general-detect.

    Shared by general-detect's batch mode and the check-general-detect gate. Metadata only.
    """
    import lamindb as ln
    from clinicalomx import get_state

    from qptiff_pipeline.domain import states

    _, entry_state = states.STAGE_ENTRY["general"]
    slide_type = ln.Record.filter(name=states.SLIDE_TYPE, is_type=True).one_or_none()
    pool = list(ln.Record.filter(type=slide_type).all()) if slide_type else []
    uids = [s.uid for s in pool if get_state(s) == entry_state]
    return uids[:limit] if limit else uids

