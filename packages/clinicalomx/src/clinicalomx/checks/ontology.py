"""Ontology version provenance: every term the instance holds is at a DECLARED release."""
from __future__ import annotations

from dataclasses import dataclass

from clinicalomx.checks import Check, Snapshot
from clinicalomx.vocab.registry import ENTRY_POINT_GROUP, FOUNDATION, Vocabulary

#: How many term ids a finding names before it gives the count alone.
EXAMPLES = 3


@dataclass(frozen=True)
class Pin:
    """One declared ontology release, and who declared it."""

    entity: str
    source: str
    version: str
    organism: str | None
    relink: bool
    owner: str

    @property
    def label(self) -> str:
        at = f"{self.source} {self.version}"
        return f"{at} ({self.organism})" if self.organism else at


def declared_pins() -> tuple[dict[str, Pin], list[str]]:
    """``({entity: Pin}, findings)`` over the foundation lockfile and every consumer vocabulary.

    A vocabulary that fails to load, and two owners pinning one entity differently, are findings.
    """
    from importlib.metadata import entry_points

    from clinicalomx.vocab.dataio import sources_lock

    findings: list[str] = []
    owned: list[tuple[str, str, dict]] = [
        (FOUNDATION, entity, spec)
        for entity, spec in (sources_lock().get("sources") or {}).items()]
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            vocab = ep.load()
        except Exception as exc:  # noqa: BLE001 - reported: its pins are unseen, not absent
            findings.append(f"vocabulary {ep.name!r} could not be loaded, so its ontology pins "
                            f"are unseen: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(vocab, Vocabulary):
            findings.append(f"vocabulary {ep.name!r} is a {type(vocab).__name__}, not a "
                            "Vocabulary, so its ontology pins are unseen")
            continue
        owned += [(vocab.owner or ep.name, e, s) for e, s in vocab.sources.items()]

    pins: dict[str, Pin] = {}
    for owner, entity, spec in owned:
        pin = Pin(entity, spec["source"], str(spec["version"]), spec.get("organism"),
                  bool(spec.get("relink")), owner)
        prior = pins.setdefault(entity, pin)
        if (prior.source, prior.version, prior.organism) != (pin.source, pin.version,
                                                             pin.organism):
            findings.append(f"bionty.{entity}: pinned at {prior.label} by {prior.owner} and at "
                            f"{pin.label} by {pin.owner}; one instance can hold only one")
    return pins, findings


def _bionty():
    """The bionty module when the connected instance carries it, else ``None``."""
    import lamindb as ln

    if "bionty" not in ln.setup.settings.instance.modules:
        return None
    import bionty as bt

    return bt


def _held(registry) -> list[tuple[int | None, str | None, str | None, str, int, list[str]]]:
    """``(source id, ontology, record organism, what it names, count, example ids)`` per group.

    Grouped by organism too where the rows carry one.
    """
    from django.db.models import Count

    id_field = getattr(registry, "_ontology_id_field", "name")
    keys = ["source_id", "source__name", "source__version", "source__organism"]
    if hasattr(registry, "organism_id"):
        keys.append("organism__name")
    groups = registry.filter().order_by().values(*keys).annotate(n=Count("id"))
    out = []
    for g in groups:
        organism = g.get("organism__name")
        if g["source_id"] is None:
            names = "no recorded source"
        else:
            names = f"{g['source__name']} {g['source__version']}"
            if g["source__organism"] not in (None, "", "all"):  # "all": not organism-specific
                names += f" ({g['source__organism']})"
        if organism and organism != g["source__organism"]:
            names += f" for organism {organism}"
        match = {"source_id": g["source_id"]}
        if "organism__name" in keys:
            match["organism__name"] = organism
        ids = (registry.filter(**match).order_by(id_field)
               .values_list(id_field, flat=True)[:EXAMPLES])
        out.append((g["source_id"], g["source__name"], organism, names, g["n"],
                    [str(i) for i in ids]))
    return sorted(out, key=lambda g: g[3])


def _examples(n: int, ids: list[str]) -> str:
    more = f", ... {n - len(ids)} more" if n > len(ids) else ""
    return f"e.g. {', '.join(ids)}{more}"


def pin_mismatches(pins: dict[str, Pin], bt) -> list[str]:
    """Every record of a pinned entity that is not at its pin, grouped by what it points at."""
    out: list[str] = []
    for entity, pin in sorted(pins.items()):
        head = f"bionty.{entity} declared {pin.label} by {pin.owner}"
        registry = getattr(bt, entity, None)
        if registry is None:
            out.append(f"{head}: bionty has no such registry, so the pin cannot be checked")
            continue
        try:
            held = _held(registry)
        except Exception as exc:  # noqa: BLE001 - reported: an unread registry is not a pass
            out.append(f"{head}: records could not be read: {type(exc).__name__}: {exc}")
            continue
        query = {"entity": f"bionty.{entity}", "name": pin.source, "version": pin.version}
        if pin.organism:
            query["organism"] = pin.organism
        pinned = list(bt.Source.filter(**query).values_list("id", "organism"))
        total = sum(g[4] for g in held)
        if len(pinned) != 1:
            state = "no Source row" if not pinned else f"{len(pinned)} Source rows"
            out.append(f"{head}: the instance holds {state} for it, so none of its {total} "
                       "record(s) can be shown to be at the pin; run the owner's ensure()")
            continue
        (pinned_id, pinned_organism), = pinned
        for source_id, ontology, organism, names, n, ids in held:
            if source_id == pinned_id:
                continue
            if source_id is not None and (
                    ontology != pin.source
                    or (hasattr(registry, "organism_id") and organism != pinned_organism)):
                out.append(f"{head}: {n} record(s) are held under {names} "
                           f"({_examples(n, ids)}), which no pin declares")
                continue
            if source_id is None:
                fix = ("an import that recorded no version; ensure() does not move a record "
                       "with no source, so relinking it is the owner's decision")
            elif pin.relink:
                fix = "the pin declares relink, so the owner's ensure() moves them, all or none"
            else:
                fix = "the pin does not declare relink, so moving them is the owner's decision"
            out.append(f"{head}: {n} record(s) point at {names} ({_examples(n, ids)}); {fix}")
    return out


def _registries(bt) -> dict[str, type]:
    """``{entity: registry}`` for every bionty registry whose rows carry a ``source``."""
    from bionty.models import BioRecord

    return {name: obj for name in dir(bt)
            if isinstance(obj := getattr(bt, name), type) and issubclass(obj, BioRecord)
            and obj is not BioRecord and not obj._meta.abstract}


def undeclared_ontologies(pins: dict[str, Pin], bt) -> list[str]:
    """Every populated bionty registry no vocabulary pins, with what its rows point at."""
    out: list[str] = []
    for entity, registry in sorted(_registries(bt).items()):
        if entity in pins:
            continue
        try:
            held = _held(registry)
        except Exception as exc:  # noqa: BLE001 - reported: an unread registry is not a pass
            out.append(f"bionty.{entity}: records could not be read: {type(exc).__name__}: {exc}")
            continue
        for _, _, _, names, n, ids in held:
            out.append(f"bionty.{entity}: {n} record(s) point at {names} "
                       f"({_examples(n, ids)}) and no vocabulary declares a version for it")
    return out


def ontology_terms_are_at_the_pin(snap: Snapshot) -> list[str]:
    """Every record of a pinned ontology is at the declared release."""
    pins, findings = declared_pins()
    try:
        bt = _bionty()
    except Exception as exc:  # noqa: BLE001 - reported: an unread instance is not a pass
        return [*findings, f"bionty could not be reached, so {len(pins)} pin(s) are unchecked: "
                           f"{type(exc).__name__}: {exc}"]
    if bt is None:
        return [*findings, *(f"bionty.{e} declared {p.label} by {p.owner}: the instance does "
                             "not carry the bionty module" for e, p in sorted(pins.items()))]
    return [*findings, *pin_mismatches(pins, bt)]


def populated_ontologies_are_declared(snap: Snapshot) -> list[str]:
    """Every populated ontology has a declared release (``vocab audit``'s unowned sources)."""
    pins, _ = declared_pins()  # load failures are reported by the error check
    try:
        bt = _bionty()
    except Exception as exc:  # noqa: BLE001 - reported: an unread instance is not a pass
        return [f"bionty could not be reached, so no populated ontology is checked: "
                f"{type(exc).__name__}: {exc}"]
    return [] if bt is None else undeclared_ontologies(pins, bt)


def ontology_checks() -> list[Check]:
    """Ontology version provenance: declared release against what the rows point at."""
    return [
        Check("vocab.ontology_terms_are_at_the_pin", "error", ontology_terms_are_at_the_pin,
              "every record of a pinned ontology points at the declared release"),
        Check("vocab.populated_ontologies_are_declared", "warn",
              populated_ontologies_are_declared,
              "every populated ontology has a declared release"),
    ]
