"""Link an ROI to its patient's OMOP `person` row, and join ROI features to patient data.

1. `propose`: the assignment is a claim in the `patient` ledger domain, cited to its source.
   Humans and agents call the same function; the store session records who acted.
2. `project`: the ROI's `patient` feature is rebuilt from the accepted claims, never edited.
   It points at the `person` record, a `Patient`.
3. `training_frame`: one row per analysis-grade ROI, its features as X and its patient's OMOP
   values as Y. An ROI with no patient, or several, is refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from clinicalomx.names import FEATURES, TYPES
from clinicalomx_ledger.grammar.model import (
    Claim,
    ClaimDomain,
    ClaimPredicate,
    ClaimSource,
    Decision,
    SourceInvalidation,
)

from .cdm import CURRENT, Release


class PatientSource(ClaimSource):
    """Where a patient assignment was read from."""

    SLIDE_MAP = "slide_map"
    ACCESSION_SHEET = "accession_sheet"
    HUMAN = "human"


class PatientPredicate(ClaimPredicate):
    """What a claim about an ROI's patient asserts. Part of every claim id: never rename."""

    PATIENT = "patient"


class PatientClaim(Claim[PatientSource, PatientPredicate], entity="ROI"):
    """One assertion that an ROI came from one patient. `value` is the OMOP `person_id`."""


class PatientDecision(Decision[PatientPredicate], entity="ROI"):
    """One ruling on a patient assignment."""


class PatientSourceInvalidation(SourceInvalidation[PatientSource], entity="ROI"):
    """A slide map or sheet voided with no replacement value."""


PATIENT_DOMAIN = ClaimDomain(
    name="patient",
    claim=PatientClaim,
    decision=PatientDecision,
    invalidation=PatientSourceInvalidation,
    subject_types=(TYPES.CORE, TYPES.TMA, TYPES.WHOLE_SLIDE, TYPES.TILE),
    reviewed_predicates=(PatientPredicate.PATIENT,),
    claim_type="PatientClaim",
    verdict_type="PatientVerdict",
    invalidation_type="PatientSourceInvalidation",
    state_feature="patient_review_state",
)


class NotAnAnalysisUnit(ValueError):
    """An ROI that is not exactly one patient's, where one patient's is required."""


def _person_type(release: Release):
    import lamindb as ln

    return ln.Record.get(name=release.type_name("person"), is_type=True)


def add_row(release: Release, table: str, values: dict):
    """Write one CDM row as a Record of its table's type, validated by its schema on save.

    The record is named by the table's primary key, or by `person_id` for a table without one. A
    taken name, or a `person_id` naming no person, is refused before anything is written. The
    save is one transaction.
    """
    import lamindb as ln
    from django.db import transaction

    key = next((f.name for f in release.table(table).fields if f.primary_key), "person_id")
    record_type = ln.Record.get(name=release.type_name(table), is_type=True)
    if ln.Record.filter(type=record_type, name=str(values[key])).exists():
        raise ValueError(f"a {record_type.name} row already has {key}={values[key]}")
    if table != "person" and not ln.Record.filter(
            type=_person_type(release), name=str(values.get("person_id"))).exists():
        raise LookupError(
            f"no {release.type_name('person')} row has person_id={values.get('person_id')}")
    with transaction.atomic():
        return ln.Record(name=str(values[key]), type=record_type, features=values).save()


def propose(store, *, roi: str, person_id: int, source: PatientSource, source_id: str,
            evidence: str, source_version: str = "", confidence: float = 1.0,
            release: Release = CURRENT) -> Claim:
    """Assert that `roi` came from the patient with OMOP `person_id`. The claim is unresolved
    until a human accepts it with `store.decide`. Refused when no such person exists."""
    import lamindb as ln

    if not ln.Record.filter(type=_person_type(release), name=str(person_id)).exists():
        raise LookupError(f"no {release.type_name('person')} row has person_id={person_id}")
    return store.add(PatientClaim(
        subject=roi, predicate=PatientPredicate.PATIENT, value=str(person_id),
        source=source, source_id=source_id, source_version=source_version,
        evidence=evidence, confidence=confidence))


def project(store, roi, *, release: Release = CURRENT):
    """Rebuild `roi`'s `patient` feature from its accepted claims. Returns the person or None.

    Several accepted patients is refused: assign onto the ROI's children instead.
    """
    import lamindb as ln
    from clinicalomx.features import upsert_values

    accepted = sorted({d.value for d in store.answers(roi.name,
                                                      predicate=PatientPredicate.PATIENT)})
    if len(accepted) > 1:
        raise NotAnAnalysisUnit(
            f"{roi.name} has accepted claims for persons {', '.join(accepted)}. An ROI defines "
            "at most one patient; assign each of its children instead.")
    if not accepted:
        if FEATURES.PATIENT in roi.features.get_values():
            roi.features.remove_values(FEATURES.PATIENT)
        return None
    person = ln.Record.get(type=_person_type(release), name=accepted[0])
    upsert_values(roi.features, {FEATURES.PATIENT: person})
    return person


def _rows_by_person(release: Release, table: str) -> dict[str, list[dict]]:
    import lamindb as ln

    rows: dict[str, list[dict]] = {}
    record_type = ln.Record.get(name=release.type_name(table), is_type=True)
    # One values read per row; batch it past ~100k rows.
    for record in ln.Record.filter(type=record_type):
        values = record.features.get_values()
        rows.setdefault(str(values["person_id"]), []).append(values)
    return rows


def training_frame(rois: Iterable, *, x: Sequence[str], y: Sequence[tuple[str, str]],
                   release: Release = CURRENT):
    """One row per ROI: `roi`, `person_id`, then each `x` feature, then each `y` value.

    `y` names `(table, field)` pairs. A `person` field is a scalar; any other table gives a list
    over the patient's rows, in no order, empty when there are none.
    """
    import pandas as pd

    rois = list(rois)
    refused, rows = [], []
    for roi in rois:
        values = roi.features.get_values()
        patient = values.get(FEATURES.PATIENT)
        if not isinstance(patient, str):
            refused.append(f"{roi.name} ({'no patient' if patient is None else 'several'})")
            continue
        rows.append({"roi": roi.name, "person_id": patient,
                     **{name: values.get(name) for name in x}})
    if refused:
        raise NotAnAnalysisUnit(
            "A training row must be one patient's ROI. Refused: " + "; ".join(refused)
            + ". Pass a container's children, which are the analysis units.")

    by_table = {t: _rows_by_person(release, t) for t in dict.fromkeys(t for t, _ in y)}
    for row in rows:
        for table, field in y:
            found = by_table[table].get(row["person_id"], [])
            column = f"{table}.{field}"
            row[column] = found[0].get(field) if table == "person" else [
                r.get(field) for r in found]
    return pd.DataFrame(rows)
