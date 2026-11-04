"""The reagent inventory checked against the marker registry.

An antibody's ``canonical_name`` must be a marker, and an IsoId's parent accession must be a
candidate of that marker's target claims. :func:`validate_conjugates` is the import gate.
Every row source is injectable.
"""

from __future__ import annotations

import re

from clinicalomx_reagents.inventory import (
    Rows,
    akoya_set_problems,
    canonical_names,
    file_problems,
    key_problems,
    label,
)
from clinicalomx_reagents.rows import antibodies as _antibody_rows
from clinicalomx_reagents.rows import barcodes as _barcode_rows
from clinicalomx_reagents.rows import fluorophores as _fluorophore_rows
from clinicalomx_reagents.spec import UNIPROT_ISOFORM, column_problems

from ..seed_data.rows import markers as _marker_rows
from ..seed_spec import MARKERS
from .marker import MarkerClaim, MarkerPredicate
from .target import TargetSet, parent, seed_claims

_ISOFORM = re.compile(UNIPROT_ISOFORM)


def _isoform_problems(antibody_rows: Rows, targets: list[MarkerClaim],
                      known_markers: set[str]) -> list[str]:
    """An IsoId must be an isoform of a candidate in the row's marker's target claims.

    ``P08575-8`` is right for a marker targeting ``P08575`` (anti-CD45RA).
    """
    by_marker: dict[str, list[MarkerClaim]] = {}
    for claim in targets:
        if claim.predicate == MarkerPredicate.TARGET:
            by_marker.setdefault(claim.subject, []).append(claim)
    problems: list[str] = []
    for position, row in enumerate(antibody_rows, start=1):
        isoform = (row.get("uniprot_isoform") or "").strip()
        if not isoform or not _ISOFORM.fullmatch(isoform):
            continue
        target = (row.get("canonical_name") or "").strip()
        if target not in known_markers:
            continue
        name = label(row, "catalogue_number", position)
        parents: set[str] = set()
        for claim in by_marker.get(target, []):
            try:
                parents |= TargetSet.parse(claim.value).parents
            except ValueError as unreadable:
                problems.append(
                    f"antibodies.csv: {name} carries isoform {isoform!r} but marker "
                    f"{target!r} has an unreadable target claim {claim.value!r}: {unreadable}"
                )
        if not parents:
            problems.append(
                f"antibodies.csv: {name} carries isoform {isoform!r} but marker {target!r} "
                "has no readable target claim, so its parent accession cannot be checked"
            )
        elif parent(isoform) not in parents:
            problems.append(
                f"antibodies.csv: {name} carries isoform {isoform!r} whose parent accession "
                f"is not among the target candidates of marker {target!r}: "
                f"{', '.join(sorted(parents))}"
            )
    return problems


def conjugate_problems(
    *,
    antibodies: Rows | None = None,
    barcodes: Rows | None = None,
    fluorophores: Rows | None = None,
    markers: Rows | None = None,
    targets: list[MarkerClaim] | None = None,
) -> list[str]:
    """Every problem with an inventory, in report order: cells, keys, isoforms, Akoya sets.

    ``targets`` defaults to the seeded target claims. ``tests/test_reagent_inventory.py`` pins the
    report.
    """
    antibody_rows = _antibody_rows() if antibodies is None else antibodies
    barcode_rows = _barcode_rows() if barcodes is None else barcodes
    fluorophore_rows = _fluorophore_rows() if fluorophores is None else fluorophores
    marker_rows = _marker_rows() if markers is None else markers
    known_markers = canonical_names(marker_rows)

    return (
        file_problems(antibody_rows, barcode_rows, fluorophore_rows)
        + column_problems(MARKERS, marker_rows)
        + key_problems(antibody_rows, barcode_rows, fluorophore_rows, known_markers)
        + _isoform_problems(antibody_rows, seed_claims() if targets is None else targets,
                            known_markers)
        + akoya_set_problems(antibody_rows, barcode_rows)
    )


def validate_conjugates(
    *,
    antibodies: Rows | None = None,
    barcodes: Rows | None = None,
    fluorophores: Rows | None = None,
    markers: Rows | None = None,
    targets: list[MarkerClaim] | None = None,
) -> None:
    """Raise unless the inventory can be imported."""
    problems = conjugate_problems(
        antibodies=antibodies,
        barcodes=barcodes,
        fluorophores=fluorophores,
        markers=markers,
        targets=targets,
    )
    if problems:
        raise ValueError("conjugate inventory is not importable:\n- " + "\n- ".join(problems))
