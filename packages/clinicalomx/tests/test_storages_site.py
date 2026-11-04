"""Storage roots are site values: declared as ${NAME}, expanded at read, refused when unset."""

import pytest

from clinicalomx.vocab.dataio import UnsetSiteValue, storages

SITE = {
    "CLINICALOMX_SITE_RAW_ROOT": "/data/raw",
    "CLINICALOMX_SITE_RAW_SCRATCH_ROOT": "/data/raw-scratch",
    "CLINICALOMX_SITE_HPC_ROOT": "/hpc/project/dbstorage",
}


def test_every_declared_root_is_a_site_value():
    assert all(row["root"].startswith("${CLINICALOMX_SITE_") for row in storages(expand=False))


def test_a_root_expands_to_the_site_value(monkeypatch):
    for name, value in SITE.items():
        monkeypatch.setenv(name, value)
    assert {row["key"]: row["root"] for row in storages()} == {
        "q6932": "/data/raw", "q8998": "/data/raw-scratch", "hpc_file": "/hpc/project/dbstorage"}


def test_an_unset_site_value_is_refused_by_name(monkeypatch):
    for name, value in SITE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLINICALOMX_SITE_HPC_ROOT")
    with pytest.raises(UnsetSiteValue, match="CLINICALOMX_SITE_HPC_ROOT"):
        storages()
