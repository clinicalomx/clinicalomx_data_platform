"""This instance's governance table, resolved through ``clinicalomx.governance.permits_*``.

Also checks that every flow resolving a policy loads the table itself: the HPC and the VM run a git
clone with no entry point.
"""
from __future__ import annotations

import pathlib

import pytest
from clinicalomx import governance as gov

# Source reads are anchored here, so the suite runs from any directory.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline"

from qptiff_pipeline.domain.governance import POLICIES


@pytest.fixture(autouse=True)
def _clear_cache():
    """Drop the framework's cached table, so each test resolves it fresh."""
    gov.reset_cache()
    yield
    gov.reset_cache()


def test_the_entry_point_actually_resolves_this_repo_s_table():
    assert set(gov.declared_folders()) >= {p.folder for p in POLICIES}


@pytest.mark.parametrize(
    "key",
    [
        "cardiac_projects/SLIDE_002_TMA3.qptiff",       # declared readable, ingest denied
        "unheard_of_folder/scan.qptiff",                # never declared
        "scan_at_the_root.qptiff",                      # no folder at all
        "",                                             # empty
    ],
)
def test_undeclared_or_denied_folders_permit_no_training(key):
    assert not gov.permits_training(key)


@pytest.mark.parametrize("key", ["unheard_of_folder/scan.qptiff", "scan_at_the_root.qptiff", ""])
def test_undeclared_folders_are_not_even_readable(key):
    assert not gov.permits_read(key)
    assert not gov.permits_ingest(key)


def test_readable_without_ingest_is_expressible():
    key = "qc_and_testing/calibration.qptiff"
    assert gov.permits_read(key), "the bytes may be probed for corruption"
    assert not gov.permits_ingest(key), "but not registered as an Artifact"


def test_training_permission_is_not_implied_by_ingest():
    key = "external_projects/scan.qptiff"
    assert gov.permits_ingest(key) and gov.permits_derive(key)
    assert not gov.permits_training(key)


def test_every_declared_policy_records_a_basis():
    missing = [p.folder for p in POLICIES if not p.basis.strip()]
    assert not missing, f"policies with no basis: {missing}"


def test_declared_folders_are_unique():
    folders = [p.folder for p in POLICIES]
    assert len(folders) == len(set(folders))


def test_the_table_imports_without_a_database():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c",
         ("import qptiff_pipeline.domain.governance, sys;"
          "print('yes' if 'lamindb' in sys.modules else '')")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "importing the policy table pulled in lamindb"


# The table must be loaded, not only declared.
def test_every_flow_that_resolves_a_policy_also_registers():
    import ast

    resolvers = ("permits_read", "permits_ingest", "permits_training", "permits_derive", "policy_for")
    flows = SRC / "flows"
    offenders = []
    for path in sorted(flows.glob("*.py")):
        tree = ast.parse(path.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        if not names & set(resolvers):
            continue
        if "register" not in names:
            offenders.append(path.name)
    assert not offenders, f"resolve a governance policy without loading the table: {offenders}"


def test_data_tests_registers_for_the_checks_it_runs():
    import ast

    tree = ast.parse((SRC / "flows/data_tests.py").read_text())
    assert "register" in {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def test_registering_is_idempotent_so_a_task_may_call_it_per_file():
    from qptiff_pipeline.domain import governance as declared

    declared.register()
    declared.register()
    assert gov.permits_read("projects/x.qptiff") is True


# Ingest is enforced, not only reported.
def test_register_qptiffs_gates_on_permits_ingest():
    import ast

    src = (SRC / "flows/register_qptiffs.py").read_text()
    attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "permits_ingest" in attrs, "register-qptiffs does not gate on the ingest policy"
    assert "register" in attrs, "the policy table is never loaded, so everything resolves UNDECLARED"


def test_the_scan_path_is_the_storage_root_so_policy_decides_scope():
    import yaml

    doc = yaml.safe_load((SRC.parents[1] / "prefect.yaml").read_text())
    params = next(d["parameters"] for d in doc["deployments"]
                  if d["name"] == "pipeline-register-qptiffs")
    assert params["source_directory"] == params["parent_storage_root"], params


def test_every_declared_folder_permits_at_least_reading():
    for p in POLICIES:
        assert not p.permits_nothing, p.folder


def test_every_flag_is_enforced_at_a_write_site():
    import ast

    flows = SRC / "flows"
    enforced = {}
    for path in sorted(flows.glob("*.py")):
        attrs = {n.attr for n in ast.walk(ast.parse(path.read_text()))
                 if isinstance(n, ast.Attribute)}
        for flag in ("permits_read", "permits_ingest", "permits_derive", "permits_training"):
            if flag in attrs:
                enforced.setdefault(flag, []).append(path.name)
    # training is enforced through policy_for(...).training, not a permits_ helper
    if "permits_training" not in enforced:
        for path in sorted(flows.glob("*.py")):
            src = path.read_text()
            if "policy_for" in src and "training" in src:
                enforced.setdefault("permits_training", []).append(path.name)

    missing = [f for f in ("permits_read", "permits_ingest", "permits_derive", "permits_training")
               if f not in enforced]
    assert not missing, f"declared but enforced nowhere: {missing} (enforced: {enforced})"


def test_a_flow_that_resolves_derive_loads_the_table_first():
    import ast

    src = (SRC / "flows/bgnorm_stage.py").read_text()
    attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "permits_derive" in attrs
    assert "register" in attrs, "the policy table is never loaded, so every core would be denied"


# Artifact.suffix is a filename, not a label.
def test_register_qptiffs_never_assigns_artifact_suffix():
    import ast

    src = (SRC / "flows/register_qptiffs.py").read_text()
    assigns = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "suffix" for t in n.targets)
    ]
    assert not assigns, "assigning Artifact.suffix renames the file on storage"


# The output root is governed by the same table.
#: A slide, TMA and core to drive the key builders with.
_QPTIFF_KEY = "projects/YTMAs 2026/Arutha_YTMA 404-2_Scan1-16-bit.qptiff"
_CORE_NAME = "Arutha_YTMA 404-2_Scan1-16-bit.qptiff:TMA0:R0C6"
_RUN_ID = "0199c1f4-6a2e-7b31-9d5c-2f8a41c0be77"


def _output_keys() -> dict[str, str]:
    """Representative output keys, built by the functions the flows write through."""
    from qptiff_pipeline.domain import keys

    return {
        "bgnormed": keys.bgnorm_key(_QPTIFF_KEY, _CORE_NAME, _RUN_ID),
        "bgnorm_qc": keys.bgnorm_qc_key(_QPTIFF_KEY, _CORE_NAME, _RUN_ID),
    }


def test_the_pipeline_writes_into_exactly_one_governed_folder():
    from qptiff_pipeline.domain import keys

    built = {
        keys.slide_dir(_QPTIFF_KEY),
        keys.tma_dir(_QPTIFF_KEY, _CORE_NAME),
        keys.core_dir(_QPTIFF_KEY, _CORE_NAME),
        keys.general_key(_QPTIFF_KEY, "rois"),
        keys.general_key(_QPTIFF_KEY, "pred", "modeluid"),
        keys.general_accepted_key(_QPTIFF_KEY, "runuid"),
        keys.core_key(_QPTIFF_KEY, _CORE_NAME, "cores"),
        keys.core_accepted_key(_QPTIFF_KEY, _CORE_NAME, "runuid"),
        keys.qptiff_thumbnail_key(_QPTIFF_KEY),
        *_output_keys().values(),
    }
    folders = {gov.folder_of(k) for k in built}
    assert folders == {keys.PIPELINE_ROOT}, folders


@pytest.mark.parametrize("role", ["bgnormed", "bgnorm_qc"])
def test_a_pipeline_output_key_is_readable(role):
    from qptiff_pipeline.domain.governance import register

    register()
    assert gov.permits_read(_output_keys()[role])


@pytest.mark.parametrize("role", ["bgnormed", "bgnorm_qc"])
def test_availability_reports_a_pipeline_output_as_present_not_denied(role):
    from clinicalomx.extract.availability import _Context

    from qptiff_pipeline.domain.governance import register

    register()
    key = _output_keys()[role]
    ctx = _Context(snap=None, declared=frozenset(), owners={}, backrefs={key: {}},
                   bytes_checked=False)
    assert ctx.state_of_key(key) == "present"


def test_the_output_folder_permits_no_training():
    from qptiff_pipeline.domain import keys
    from qptiff_pipeline.domain.governance import register

    register()
    assert not gov.permits_training(f"{keys.PIPELINE_ROOT}/x")
    assert gov.permits_derive(f"{keys.PIPELINE_ROOT}/x")


def test_harvest_feedback_gates_on_the_source_key_not_the_output_key():
    import ast

    tree = ast.parse((SRC / "flows/harvest_feedback.py").read_text())
    gate = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "policy_for")
    assert [a.id for a in gate.args if isinstance(a, ast.Name)] == ["source_key"]


def test_no_existing_folder_carries_the_output_folder_s_name():
    from qptiff_pipeline.domain import keys

    assert [p.folder for p in POLICIES].count(keys.PIPELINE_ROOT) == 1
