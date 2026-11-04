"""UniProt REST: fetch and parse. No model, no judgement."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .snapshot import FetchedContent, Transport, http

BASE = "https://rest.uniprot.org/uniprotkb"
FIELDS = "accession,protein_name,cc_subcellular_location,cc_function,go_c,gene_names"

#: The scope of an annotation that applies to the entry rather than to one named isoform.
ENTRY_LEVEL = ""


def url_for(accession: str) -> str:
    return f"{BASE}/{accession}.json?fields={FIELDS}"


@dataclass(frozen=True)
class Localisation:
    """One `SUBCELLULAR LOCATION` comment: its scope and its locations.

    `scope` is the comment's `molecule`, such as `Isoform 2`; `ENTRY_LEVEL` ("") when absent,
    meaning the whole entry.
    """

    scope: str = ENTRY_LEVEL
    locations: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniProtRecord:
    accession: str
    protein_name: str
    gene_names: tuple[str, ...] = ()
    #: Gene synonyms, as UniProt lists them. Alias material, not identity.
    gene_synonyms: tuple[str, ...] = ()
    #: `alternativeNames` full names. Short names, CD antigen names and `contains` are discarded.
    alternative_names: tuple[str, ...] = ()
    #: Subcellular locations, one entry per comment, in UniProt's order. Read through
    #: `locations_for`. Topology text is discarded.
    localisations: tuple[Localisation, ...] = ()
    #: `FUNCTION` comment text, joined in UniProt's order. Function scope is not modelled.
    function: str = ""
    #: GO cellular-component terms only, as `GO:xxxxxxx` ids.
    go_cc: tuple[str, ...] = ()
    #: Release the body was served from, when the response says.
    release: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def locations(self) -> tuple[str, ...]:
        """Every location, all scopes flattened, in UniProt's order."""
        return tuple(loc for block in self.localisations for loc in block.locations)

    @property
    def scopes(self) -> tuple[str, ...]:
        """The scopes annotated, in order. `ENTRY_LEVEL` for an unscoped comment."""
        return tuple(dict.fromkeys(block.scope for block in self.localisations))

    def locations_for(self, scope: str = ENTRY_LEVEL) -> tuple[str, ...]:
        """Locations annotated to exactly `scope`, matched verbatim (``"Isoform 2"``).

        Default is the entry level. An unannotated scope gives an empty tuple.
        """
        return tuple(loc for block in self.localisations
                     if block.scope == scope for loc in block.locations)

    @property
    def evidence(self) -> str:
        """Verbatim location text for a citation to quote. Scoped comments keep UniProt's
        ``[Isoform 2]: Secreted`` prefix."""
        return ". ".join(
            f"[{block.scope}]: {'. '.join(block.locations)}" if block.scope
            else ". ".join(block.locations)
            for block in self.localisations
        )


def parse(fetched: FetchedContent) -> UniProtRecord:
    body = json.loads(fetched.text)
    comments = body.get("comments", [])

    localisations = [
        Localisation(
            scope=comment.get("molecule") or ENTRY_LEVEL,
            locations=tuple(
                loc["location"]["value"]
                for loc in comment.get("subcellularLocations", [])
                if loc.get("location", {}).get("value")
            ),
        )
        for comment in comments
        if comment.get("commentType") == "SUBCELLULAR LOCATION"
    ]

    function = " ".join(
        text["value"]
        for comment in comments
        if comment.get("commentType") == "FUNCTION"
        for text in comment.get("texts", [])
        if text.get("value")
    )

    description = body.get("proteinDescription", {})
    alternative_names = [
        alt["fullName"]["value"]
        for alt in description.get("alternativeNames", [])
        if alt.get("fullName", {}).get("value")
    ]

    genes = body.get("genes", [])
    gene_synonyms = [
        syn["value"] for gene in genes for syn in gene.get("synonyms", []) if syn.get("value")
    ]

    # GO terms are prefixed by aspect: C: cellular component, F: function, P: process.
    go_cc = [
        ref["id"]
        for ref in body.get("uniProtKBCrossReferences", [])
        if ref.get("database") == "GO"
        and any(p.get("key") == "GoTerm" and str(p.get("value", "")).startswith("C:")
                for p in ref.get("properties", []))
    ]

    return UniProtRecord(
        accession=body.get("primaryAccession", ""),
        protein_name=(description.get("recommendedName", {})
                      .get("fullName", {}).get("value", "")),
        gene_names=tuple(g.get("geneName", {}).get("value", "") for g in genes),
        gene_synonyms=tuple(gene_synonyms),
        alternative_names=tuple(alternative_names),
        localisations=tuple(block for block in localisations if block.locations),
        function=function,
        go_cc=tuple(go_cc),
        release=fetched.headers.get("x-uniprot-release", ""),
        raw=body,
    )


def fetch(accession: str, *, transport: Transport = http) -> tuple[UniProtRecord, FetchedContent]:
    """The parsed record and the raw response, so the raw can be snapshotted."""
    fetched = transport(url_for(accession))
    return parse(fetched), fetched
