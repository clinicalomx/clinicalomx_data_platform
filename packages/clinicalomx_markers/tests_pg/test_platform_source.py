"""A channel's platform resolves through EFO at the pinned release, and audit owns EFO."""

from __future__ import annotations

import argparse

import bionty as bt
import lamindb as ln
import pytest
from clinicalomx.vocab.__main__ import cmd_audit

from clinicalomx_markers.curate.channels import platform_term
from clinicalomx_markers.domain.imaging_channel import PLATFORM_SOURCE, PLATFORM_TERMS
from clinicalomx_markers.vocab import VOCAB


def test_the_declared_pin_is_the_release_the_instance_holds():
    declared = VOCAB.sources["ExperimentalFactor"]
    held = set(bt.ExperimentalFactor.filter().values_list("source__version", flat=True))
    assert held == {declared["version"]}


@pytest.mark.parametrize("label", sorted(PLATFORM_TERMS))
def test_every_platform_label_resolves_through_the_declared_source(label):
    term = platform_term(label)
    assert term.ontology_id == PLATFORM_TERMS[label]
    assert (term.source.name, term.source.version) == (PLATFORM_SOURCE["source"],
                                                        PLATFORM_SOURCE["version"])


def test_a_platform_with_no_term_at_the_pin_is_refused():
    """CosMx has no EFO term at the pinned release."""
    with pytest.raises(ValueError, match="no EFO term"):
        platform_term("CosMx")


@pytest.fixture(autouse=True)
def _site(monkeypatch):
    """The audit reads storage roots, which are site values; these are synthetic."""
    for name, value in {"CLINICALOMX_SITE_RAW_ROOT": "/data/raw",
                        "CLINICALOMX_SITE_RAW_SCRATCH_ROOT": "/data/raw-scratch",
                        "CLINICALOMX_SITE_HPC_ROOT": "/hpc/project/dbstorage"}.items():
        monkeypatch.setenv(name, value)


def _audit(capsys) -> str:
    cmd_audit(argparse.Namespace(strict=False))
    return capsys.readouterr().out


def test_the_audit_does_not_report_efo_unowned(capsys):
    lines = [line for line in _audit(capsys).splitlines() if " source " in line]
    assert not [line for line in lines if line.split()[-1] == "ExperimentalFactor"], lines


def test_the_audit_reports_a_populated_ontology_nobody_pins(capsys):
    """Positive control for the audit."""
    # Saved on main: an earlier test can leave another branch as the default.
    bt.Tissue(name="synthetic tissue", branch=ln.Branch.get(name="main")).save()
    out = _audit(capsys)
    assert any(line.split() == ["extra", "(unowned)", "source", "Tissue"]
               for line in out.splitlines()), out
