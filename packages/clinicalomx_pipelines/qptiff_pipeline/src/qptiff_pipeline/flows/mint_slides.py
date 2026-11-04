from __future__ import annotations

# Put the checkout's src/ on sys.path: the worker may run without an install of this package.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger


def slide_copied_features() -> tuple[str, ...]:
    """The qptiff features a Slide carries a copy of, taken at mint time, in mint order.

    check-qptiffs refreshes the copies from this same list.
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.domain import vocab

    return (
        vocab.IMAGE_TYPE,
        qptiff_vocab.QPTIFF_N_CHANNELS,
        qptiff_vocab.QPTIFF_CHANNEL_NAMES,
        qptiff_vocab.QPTIFF_DIMS,
        qptiff_vocab.QPTIFF_IMAGE_TYPE,
        qptiff_vocab.QPTIFF_IS_TMA,
        qptiff_vocab.QPTIFF_SCALE_FACTOR,
        qptiff_vocab.QPTIFF_SCALE_FACTOR_UNIT,
        qptiff_vocab.QPTIFF_MAGNIFICATION,
    )


#: Of :func:`slide_copied_features`, the ones a refresh must not rewrite. Triage overwrites
#: ``image_type`` with the reviewer's answer, and a refresh would revert it.
SLIDE_REFRESH_EXCLUDED: frozenset[str] = frozenset({"image_type"})


@flow(name="mint-slides", log_prints=True)
def mint_slides(
    suffix: str = ".qptiff", limit: int | None = None, require_integrity: bool = True
) -> dict:
    """Mint a WholeSlide Record in state ``registered`` for each qptiff Artifact that has none.

    suffix: match Artifacts whose key ends with this. Not the ``Artifact.suffix`` field.
    limit: cap on slides minted this run (None = all new).
    require_integrity: mint only qptiffs check-qptiffs marked ``ok``. False bypasses the gate.
    """
    logger = get_run_logger()

    from pathlib import Path

    import lamindb as ln
    from clinicalomx import confirm

    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.lamin.queries import source_artifacts
    from qptiff_pipeline.lamin.registry import get_or_create_record, get_or_create_type
    from qptiff_pipeline.lamin.tracking import track_prefect_run
    from qptiff_pipeline.orchestration.events import emit

    track_prefect_run("mint-slides")
    try:
        slide_type = get_or_create_type(states.SLIDE_TYPE)
        # `source_image` is foundation vocabulary, created by `clinicalomx.vocab apply`.
        vocab.ensure()  # this repo's declared features (see vocab.py)

        def _mintable(artifact) -> bool:
            """Whether this qptiff can become a Slide."""
            if not require_integrity:
                return True
            integ = artifact.features.get_values().get(qptiff_vocab.INTEGRITY_FEATURE)
            return integ == qptiff_vocab.INTEGRITY_OK

        # A new slide is a registered qptiff Artifact with no Slide Record.
        minted: list[str] = []
        skipped_integrity = 0
        conflated: list[str] = []
        for qptiff in source_artifacts(suffix):
            name = Path(qptiff.key).stem
            existing = ln.Record.filter(name=name, type=slide_type).one_or_none()
            if existing is not None:
                # A name collision is not proof of the same slide: `name` drops the folder. A
                # byte-identical copy reuses the Record. A different file is refused, because it
                # would merge two scans into one Record. `source_image` returns the key, so the
                # twin is compared by hash.
                prior = existing.features.get_values().get("source_image")
                if prior is not None and prior != qptiff.key and _mintable(qptiff):
                    # `_mintable` first: a copy the integrity gate excludes cannot be merged.
                    twin = ln.Artifact.filter(key=prior).first()
                    if twin is not None and twin.hash != qptiff.hash:
                        conflated.append(f"{name}: {qptiff.key} vs the slide's {prior}")
                        logger.error(
                            f"REFUSING {name}: a Slide of this name already exists from a "
                            f"DIFFERENT qptiff ({prior}). {qptiff.key} would have merged into it "
                            f"and never been detected. Disambiguate the filenames or withdraw a copy."
                        )
                continue  # already in the pipeline
            # Integrity gate: only qptiffs check-qptiffs verified readable become Slides.
            if not _mintable(qptiff):
                integ = qptiff.features.get_values().get(qptiff_vocab.INTEGRITY_FEATURE)
                skipped_integrity += 1
                logger.info(f"skip {name}: integrity={integ!r} (not '{qptiff_vocab.INTEGRITY_OK}')")
                continue
            # Copy only the small scalar metadata triage needs. The review app reads the qptiff
            # thumbnail through `slide.source_image`.
            qvals = qptiff.features.get_values()
            slide_features = {"source_image": qptiff}
            # The file's own answers travel with the inferred ones. The metadata blob and the
            # channel Records stay on the qptiff Artifact.
            for f in slide_copied_features():
                if qvals.get(f) is not None:
                    slide_features[f] = qvals[f]
            slide = get_or_create_record(
                name=name, type=slide_type, feature_values=slide_features
            )
            # Set state registered and emit clinical-roi.slide.registered, which starts detection.
            confirm(slide, states.S.REGISTERED, event=states.ENTRY_EVENTS["general"], emit=emit)
            minted.append(name)
            logger.info(f"minted Slide {name} (registered, event emitted)")
            if limit and len(minted) >= limit:
                break

        logger.info(
            f"mint-slides done: {len(minted)} new slide(s); "
            f"{skipped_integrity} skipped (integrity gate); "
            f"{len(conflated)} refused (name collision on a different file)"
        )
        return {
            "minted": len(minted),
            "slides": minted,
            "skipped_integrity": skipped_integrity,
            "conflated": conflated,
        }
    finally:
        ln.finish()
