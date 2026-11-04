"""`recover_annotated_images` against a disposable instance with images and labels but no Collections.

One label comes from a single-input run, two from a bulk run over every image. Tests share the
instance and run in file order: check, apply, apply again, check, then precondition tests that
each break one thing and restore it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import lamindb as ln
import pytest
from conftest import STORE
from croi.objectives.tma_core import recover_annotated_images as rec

SCRIPT = pathlib.Path(rec.__file__)
ROI = pathlib.Path(__file__).parents[2] / "roi_detection"  # prefect.yaml entrypoints start here

LABELS = STORE / "core_labels"
PREFIX = "core_labels/"


def _file(rel: str, data: bytes) -> pathlib.Path:
    p = STORE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _run(key: str):
    return ln.Run(transform=ln.Transform(key=key, kind="script").save()).save()


def _register(rel: str, data: bytes, run):
    return ln.Artifact(_file(rel, data), key=rel, run=run).save()


def _label(name: str, run, *inputs):
    art = _register(f"core_labels/{name}", f"0 0.5 0.5 0.2 0.2 {name}".encode(), run)
    run.input_artifacts.add(*inputs)
    return art


@pytest.fixture(scope="module")
def world():
    from clinicalomx.roi import ROI_NAME, ROI_UID, ensure_features

    ensure_features()
    roi = ln.Record(name="SlideA:TMA0").save()
    made = _run("general-extract")
    imgs = {
        "A": _register("shared/tma_crops/SlideA_tma_0.png", b"png-A", made),
        "B": _register("shared/tma_crops/SlideB_tma_0.png", b"png-B", made),
        "CD": _register("shared/pyramid_low_scale/Slide C&D.png", b"png-CD", made),
        "E": _register("shared/tma_crops/SlideE_tma_0.png", b"png-E", made),  # never labelled
    }
    imgs["A"].features.set_values({ROI_UID: roi.uid, ROI_NAME: roi.name})

    _label("0a1b-SlideA_tma_0.txt", _run("core-ingest-labels-step"), imgs["A"])
    bulk = _run("core-ingest-labels-legacy")
    _label("2c3d-SlideB_tma_0.txt", bulk, imgs["B"], imgs["CD"], imgs["E"])
    _label("4e5f-Slide_CD.txt", bulk)
    _register("core_labels/notes.json", b'{"categories": []}', _run("notes"))
    _file("core_labels/classes.txt", b"full_core\npartial_core\n")  # on disk only
    return imgs


def _script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          cwd=SCRIPT.parent, check=False)


def _status(out: str) -> dict:
    return {line[:2]: line.split()[1] for line in out.splitlines() if line[:2] in
            {f"P{i}" for i in range(1, 8)} and line[2] == " "}


def _checks():
    return {r.id: r for r in rec.run_checks(str(STORE), LABELS, PREFIX)}


def test_check_before_apply_names_apply_as_the_next_step(world):
    done = _script("check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _status(done.stdout) == {"P1": "FAIL", **{f"P{i}": "PASS" for i in range(2, 8)}}
    assert "CLEAR: next step is `apply`." in done.stdout
    assert ("P6 PASS  1 of 3 images keep a live ROI; 0 resolve by name alone; "
            "2 of 3 have NO ROI and ingest will link none") in done.stdout


def test_apply_rebuilds_the_images_the_labels_name(world):
    done = _script("apply")
    assert done.returncode == 0, done.stdout + done.stderr

    c = ln.Collection.get(key=rec.KEY, version="1")  # the lookup ingest-labels makes
    assert {a.uid for a in c.artifacts.all()} == {world[k].uid for k in ("A", "B", "CD")}
    assert c.run is not None and c.run.transform.key == "recover_annotated_images.py"

    # and ingest's own matcher pairs every label against it
    from croi.objectives.tma_core.labels import match_core_labels

    assert len(match_core_labels(LABELS, [c])) == 3


def test_second_apply_writes_nothing(world):
    before = (ln.Collection.objects.count(), ln.Run.objects.count(), ln.Transform.objects.count())
    done = _script("apply")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already exists; nothing written" in done.stdout
    assert (ln.Collection.objects.count(), ln.Run.objects.count(),
            ln.Transform.objects.count()) == before


def test_ingest_labels_runs_on_the_rebuilt_collection_and_registers_nothing(world, capsys):
    from croi.objectives.tma_core import environment, labels
    from croi.objectives.tma_core.config import IngestLabelsConfig

    before = ln.Artifact.filter(key__startswith=PREFIX).count()
    labels.run_ingest_core_labels(
        config=environment.load_config("1_ingest_labels.yaml", IngestLabelsConfig),
        project=ln.Project(name="clinical-roi-core").save(),
        output_storage=ln.Storage.get(root=str(STORE)),
    )
    assert "Matched 3 (image, label) pairs." in capsys.readouterr().out
    assert ln.Artifact.filter(key__startswith=PREFIX).count() == before
    core = ln.Collection.get(key=rec.CORE_KEY)
    assert {a.uid for a in core.artifacts.all()} >= {world[k].uid for k in ("A", "B", "CD")}
    assert core.artifacts.count() == 6


def test_check_after_apply_names_ingest_as_the_next_step(world):
    done = _script("check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert set(_status(done.stdout).values()) == {"PASS"}
    assert "Next: python -m croi tma_core ingest-labels" in done.stdout


def test_p3_reports_a_file_with_no_record_and_a_record_with_no_file(world):
    extra = _file("core_labels/9z9z-SlideZ_tma_0.txt", b"0 0.5 0.5 0.1 0.1")
    gone = LABELS / "0a1b-SlideA_tma_0.txt"
    kept = gone.read_bytes()
    gone.unlink()
    try:
        r = _checks()
        assert not r["P3"].ok and not r["P2"].ok
        text = "\n".join(r["P3"].lines)
        assert "WILL NEWLY REGISTER it and it SILENTLY JOINS" in text and extra.name in text
        assert "SILENTLY LEAVES IT OUT" in text and gone.name in text
        assert rec.verdict(list(r.values()))[0] == 1
    finally:
        extra.unlink()
        gone.write_bytes(kept)


def test_p6_does_not_call_an_instance_with_no_roi_links_safe(world):
    """An instance with no ROI links reads as no links, not as safe. Still a PASS."""
    from clinicalomx.roi import ROI_NAME, ROI_UID

    a = world["A"]
    kept = a.features.get_values()
    a.features.remove_values([ROI_UID, ROI_NAME])
    try:
        done = _script("check")
        p6 = next(line for line in done.stdout.splitlines() if line.startswith("P6 "))
        assert p6 == ("P6 PASS  0 of 3 images keep a live ROI; 0 resolve by name alone; "
                      "3 of 3 have NO ROI and ingest will link none")
        assert "safe" not in done.stdout.lower()
        assert ("no ROI: 0 dangling (roi_uid or name names a Record that is gone), "
                "3 with no ROI pointer at all") in done.stdout

        a.features.set_values({ROI_UID: "deadbeefdeadbeef", ROI_NAME: "SlideA:Gone"})
        r = _checks()["P6"]
        assert r.ok and r.title.startswith("0 of 3 images keep a live ROI")
        assert r.lines == [("no ROI: 1 dangling (roi_uid or name names a Record that is gone), "
                            "2 with no ROI pointer at all")]
    finally:
        a.features.remove_values([ROI_UID, ROI_NAME])
        a.features.set_values({ROI_UID: kept[ROI_UID], ROI_NAME: kept[ROI_NAME]})
    assert _checks()["P6"].title.startswith("1 of 3 images keep a live ROI")


def test_p6_link_by_name_waits_on_the_flag_and_an_abort_ignores_it(world):
    from clinicalomx.roi import ROI_NAME, ROI_UID, resolve_roi

    b = world["B"]
    b.features.set_values({ROI_UID: "deadbeefdeadbeef", ROI_NAME: "SlideB:TMA0"})
    # lamindb reuses a Record for a repeated name within one type, so the colliding pair uses two types.
    croi, pipeline = (ln.Record(name=n, is_type=True).save() for n in ("croi-TMA", "pipeline-TMA"))
    first = ln.Record(name="SlideB:TMA0", type=croi).save()
    try:
        assert resolve_roi(b) == first
        done = _script("check")
        assert done.returncode == 1, done.stdout + done.stderr
        assert _status(done.stdout)["P6"] == "WARN"
        assert "BY NAME ALONE" in done.stdout and first.uid in done.stdout
        assert "ACKNOWLEDGE: P6" in done.stdout and "--accept-by-name-links" in done.stdout
        assert "CLEAR" not in done.stdout

        done = _script("check", "--accept-by-name-links")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _status(done.stdout)["P6"] == "WARN" and first.uid in done.stdout
        assert "CLEAR: the collection is in place." in done.stdout

        second = ln.Record(name="SlideB:TMA0", type=pipeline).save()
        with pytest.raises(ln.errors.MultipleResultsFound):
            resolve_roi(b)
        for args in (("check",), ("check", "--accept-by-name-links")):
            done = _script(*args)
            assert done.returncode == 1, done.stdout + done.stderr
            assert _status(done.stdout)["P6"] == "FAIL" and "WILL ABORT PARTWAY" in done.stdout
            assert "STOP: P6 failed." in done.stdout
        second.delete(permanent=True)
    finally:
        b.features.remove_values([ROI_UID, ROI_NAME])
        first.delete(permanent=True)
        croi.delete(permanent=True)
        pipeline.delete(permanent=True)
    assert _checks()["P6"].ok


def test_p7_fails_and_apply_refuses_when_a_label_names_no_image(world):
    orphan = _label("6g7h-SlideQ_tma_0.txt", _run("orphan"), world["E"])
    try:
        r = _checks()["P7"]
        assert not r.ok and orphan.key in "\n".join(r.lines)
        with pytest.raises(SystemExit, match="refusing"):
            rec.build(PREFIX)
    finally:
        orphan.delete(permanent=True, storage=True)
    assert _checks()["P7"].ok



# ---------------------------------------------------------------------------------------------
# The flow: the deployment's entry point, run as a real Prefect flow run
# ---------------------------------------------------------------------------------------------

FLOW_FILE = SCRIPT.parent / "flows" / "tma_core_recover.py"


@pytest.fixture(scope="module")
def harness():
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        yield


@pytest.fixture(scope="module")
def recover_flow(harness):
    spec = importlib.util.spec_from_file_location("recover_annotated_images_flow", FLOW_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # loaded by path, as Prefect loads an entrypoint
    return mod.recover_annotated_images


def _writes():
    return (ln.Collection.objects.count(), ln.Run.objects.count(), ln.Transform.objects.count())


def _logged(caplog) -> str:
    """The run logs' lines from ``log_prints``, then cleared."""
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "prefect.flow_runs")
    caplog.clear()
    return text


def _flow_state(flow, **params):
    return flow(return_state=True, **params)


def test_flow_check_is_the_default_and_writes_nothing(world, recover_flow, caplog):
    # Start with no annotated_images Collection.
    for c in rec.existing():
        c.delete(permanent=True)
    before = _writes()
    state = _flow_state(recover_flow)
    assert state.is_completed(), state.message
    assert state.result() == "CLEAR: next step is `apply`."
    out = _logged(caplog)
    assert "P1 FAIL" in out and "P7 PASS" in out
    assert _writes() == before and not rec.existing()


def test_flow_apply_waits_on_the_by_name_flag(world, recover_flow, caplog):
    from clinicalomx.roi import ROI_NAME, ROI_UID

    b = world["B"]
    b.features.set_values({ROI_UID: "deadbeefdeadbeef", ROI_NAME: "SlideB:TMA0"})
    croi = ln.Record(name="croi-TMA", is_type=True).save()
    first = ln.Record(name="SlideB:TMA0", type=croi).save()
    try:
        before = _writes()
        state = _flow_state(recover_flow, apply=True)
        assert state.is_failed() and "ACKNOWLEDGE: P6" in state.message
        assert "P6 WARN" in _logged(caplog)
        assert _writes() == before and not rec.existing()

        state = _flow_state(recover_flow, apply=True, accept_by_name_links=True)
        assert state.is_completed(), state.message
        assert state.result() == "CLEAR: the collection is in place. Next: python -m croi tma_core ingest-labels."
        out = _logged(caplog)
        assert "saved clinical-roi/core/annotated_images v1" in out and "checks after apply:" in out
        c = ln.Collection.get(key=rec.KEY, version=rec.VERSION)
        assert {a.uid for a in c.artifacts.all()} == {world[k].uid for k in ("A", "B", "CD")}
        # Imported by the flow, the module still keys its Transform on its file name.
        assert c.run.transform.key == "recover_annotated_images.py"
    finally:
        b.features.remove_values([ROI_UID, ROI_NAME])
        first.delete(permanent=True)
        croi.delete(permanent=True)


def test_flow_second_apply_writes_nothing(world, recover_flow, caplog):
    before = _writes()
    state = _flow_state(recover_flow, apply=True)
    assert state.is_completed(), state.message
    assert "already exists; nothing written" in _logged(caplog)
    assert _writes() == before


def test_the_deployment_loads_its_entrypoint_and_writes_nothing_by_default():
    import inspect

    import yaml
    from prefect.flows import load_flow_from_entrypoint

    doc = yaml.safe_load((ROI / "prefect.yaml").read_text())
    (dep,) = [d for d in doc["deployments"] if d["name"] == "utils-recover-annotated-images"]
    loaded = load_flow_from_entrypoint(str(ROI / dep["entrypoint"]))
    defaults = {n: p.default for n, p in inspect.signature(loaded.fn).parameters.items()}
    assert defaults == {"apply": False, "accept_by_name_links": False}
    assert dep.get("parameters", {}) == defaults


# ---------------------------------------------------------------------------------------------
# The next two stages as deployments: ingest-labels-core and split-core
# ---------------------------------------------------------------------------------------------


def _deployment(name: str) -> dict:
    import yaml

    doc = yaml.safe_load((ROI / "prefect.yaml").read_text())
    (dep,) = [d for d in doc["deployments"] if d["name"] == name]
    return dep


def _stage_flow(name: str):
    """The flow a deployment names, loaded from its entrypoint the way Prefect loads it."""
    from prefect.flows import load_flow_from_entrypoint

    return load_flow_from_entrypoint(str(ROI / _deployment(name)["entrypoint"]))


@pytest.mark.parametrize("name, config", [("ingest-labels-core", "1_ingest_labels.yaml"),
                                          ("split-core", "2_split.yaml")])
def test_each_stage_deployment_takes_only_the_clis_config(name, config):
    import inspect

    loaded = _stage_flow(name)
    defaults = {n: p.default for n, p in inspect.signature(loaded.fn).parameters.items()}
    assert defaults == {"config": config}
    assert _deployment(name).get("parameters", {}) == defaults


def test_ingest_labels_flow_rebuilds_core_then_writes_nothing(world, harness, caplog):
    for c in ln.Collection.filter(key=rec.CORE_KEY):
        c.delete(permanent=True)
    flow = _stage_flow("ingest-labels-core")
    labels = ln.Artifact.filter(key__startswith=PREFIX).count()

    state = flow(return_state=True)
    assert state.is_completed(), state.message
    assert state.result().startswith("WROTE: 0 new label artifacts, clinical-roi/core: CREATED")
    out = _logged(caplog)
    assert "Matched 3 (image, label) pairs." in out  # the stage's own line
    assert ln.Artifact.filter(key__startswith=PREFIX).count() == labels
    (core,) = ln.Collection.filter(key=rec.CORE_KEY)

    state = flow(return_state=True)
    assert state.is_completed(), state.message
    assert state.result() == ("UNCHANGED: no new label artifact and no new collection version. "
                              "Next: split.")
    assert f"clinical-roi/core: unchanged uid={core.uid} with 6 artifacts" in _logged(caplog)
    assert ln.Collection.filter(key=rec.CORE_KEY).count() == 1


def test_split_flow_writes_three_splits_then_nothing(world, harness, caplog):
    flow = _stage_flow("split-core")
    state = flow(return_state=True)
    assert state.is_completed(), state.message
    assert state.result() == "WROTE: a new version of train, val, test. Next: train."
    assert "clinical-roi/core/splits/train: CREATED" in _logged(caplog)
    before = ln.Collection.objects.count()

    state = flow(return_state=True)
    assert state.is_completed(), state.message
    assert state.result() == "UNCHANGED: no new split version. Next: train."
    out = _logged(caplog)
    for name in ("train", "val", "test"):
        assert f"clinical-roi/core/splits/{name}: unchanged" in out
    assert ln.Collection.objects.count() == before
