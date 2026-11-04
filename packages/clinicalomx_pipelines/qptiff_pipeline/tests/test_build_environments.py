"""build-environments: the work list comes from the deployments, and the download cannot race.

``declared_environments`` runs against the real ``prefect.yaml`` files. The race test runs eight
real
processes at one target path. Only the network is faked, through ``httpx.MockTransport``.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import pathlib
import subprocess
import sys
import threading
import time

import httpx
import pytest

from conftest import load_flow_module

be = load_flow_module("build_environments")

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = pathlib.Path(__file__).resolve().parents[3]
TESTS = pathlib.Path(__file__).resolve().parent
DETECTOR_YAML = PACKAGES / "clinicalomx_ml" / "roi_detection" / "prefect.yaml"

REF = "oras://ghcr.io/example-org/bgnorm-inference-sif:v0.1.2-cx0.2.6"

#: The fake SIF, served in three pieces. Any prefix seen at the target path is a partial SIF.
CHUNKS = (b"HEAD", b"BODY", b"TAIL")
BODY = b"".join(CHUNKS)
DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()

ENV = be.Declared(
    name="bgnorm-inference",
    image="ghcr.io/example-org/bgnorm-inference:v0.1.2-cx0.2.6",
    sif_key="environments/example-org/bgnorm-inference_v0.1.2-cx0.2.6.sif",
    declared_by=("pipeline-bgnorm-core",),
)


#: The site values the deployments' ``image`` and ``sif_artifact`` spell as ``{{ $NAME }}``.
SITE = {"CLINICALOMX_SITE_IMAGE_REGISTRY": "ghcr.io/site-owner",
        "CLINICALOMX_SITE_IMAGE_OWNER": "site-owner"}


@pytest.fixture(autouse=True)
def _site_values(monkeypatch):
    for name, value in SITE.items():
        monkeypatch.setenv(name, value)


def _declared_sif_lines(path: pathlib.Path) -> list[str]:
    """Every ``sif_artifact:`` value in a prefect.yaml, read as plain text."""
    out = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("sif_artifact:"):
            value = stripped.split(":", 1)[1].split(" #", 1)[0].strip().strip('"')
            for name, site in SITE.items():
                value = value.replace("{{ $" + name + " }}", site)
            out.append(value)
    return out


# The work list.

def test_the_work_list_is_every_declaration_in_both_prefect_yamls() -> None:
    assert DETECTOR_YAML.exists(), "the detector prefect.yaml is the second half of the work list"
    ours = _declared_sif_lines(PKG_ROOT / "prefect.yaml")
    theirs = _declared_sif_lines(DETECTOR_YAML)
    # Each file on its own, so two empty reads cannot pass the comparison below.
    assert ours, "no sif_artifact read from the pipeline prefect.yaml"
    assert theirs, "no sif_artifact read from the detector prefect.yaml"

    declared = be.declared_environments()
    assert {e.sif_key for e in declared} == set(ours) | set(theirs)
    # One work item per distinct key, not per deployment.
    assert len(declared) == len(set(ours) | set(theirs))


def test_every_environment_carries_the_image_its_key_names() -> None:
    for env in be.declared_environments():
        owner, name = env.image.rsplit(":", 1)[0].split("/")[-2:]
        assert env.sif_key == f"environments/{owner}/{name}_{env.image.rsplit(':', 1)[1]}.sif"
        assert owner == SITE["CLINICALOMX_SITE_IMAGE_OWNER"]
        assert env.name == env.image.rsplit("/", 1)[-1].split(":")[0]
        assert env.declared_by, f"{env.name} is declared by no deployment"


def test_a_missing_prefect_yaml_is_skipped_not_fatal(tmp_path) -> None:
    ours = be.declared_environments((PKG_ROOT / "prefect.yaml", tmp_path / "absent.yaml"))
    assert {e.sif_key for e in ours} == set(_declared_sif_lines(PKG_ROOT / "prefect.yaml"))


def test_one_key_declared_for_two_images_is_refused(tmp_path) -> None:
    bad = tmp_path / "prefect.yaml"
    bad.write_text(
        "deployments:\n"
        "  - name: a\n    work_pool:\n      job_variables:\n"
        "        image: ghcr.io/o/env:v1\n        sif_artifact: environments/env_v1.sif\n"
        "  - name: b\n    work_pool:\n      job_variables:\n"
        "        image: ghcr.io/o/env:v2\n        sif_artifact: environments/env_v1.sif\n"
    )
    with pytest.raises(ValueError, match="declared for two images"):
        be.declared_environments((bad,))


def test_an_unset_site_value_is_refused_rather_than_written_into_a_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLINICALOMX_SITE_IMAGE_OWNER")
    with pytest.raises(ValueError, match="CLINICALOMX_SITE_IMAGE_OWNER is not set"):
        be.declared_environments((PKG_ROOT / "prefect.yaml",))


def test_a_sif_artifact_with_no_image_is_refused(tmp_path) -> None:
    bad = tmp_path / "prefect.yaml"
    bad.write_text(
        "deployments:\n  - name: a\n    work_pool:\n      job_variables:\n"
        "        sif_artifact: environments/env_v1.sif\n"
    )
    with pytest.raises(ValueError, match="no image"):
        be.declared_environments((bad,))


# What each environment is fetched from.

def test_the_published_sif_is_read_off_the_build_manifest() -> None:
    assert be.oras_reference(ENV, {"digest": DIGEST, "sif_oras_ref": REF}) == REF


def test_an_environment_with_no_published_sif_is_refused_not_converted() -> None:
    with pytest.raises(ValueError, match="publishes no ORAS SIF"):
        be.oras_reference(ENV, {"digest": DIGEST})


def test_a_missing_manifest_says_which_step_produces_one() -> None:
    with pytest.raises(ValueError, match="section 2.7"):
        be.oras_reference(ENV, None)


def test_a_malformed_manifest_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="sha256"):
        be.oras_reference(ENV, {"digest": "sha256:nope", "sif_oras_ref": REF})


def test_a_bad_oras_reference_fails_before_the_lock_is_taken() -> None:
    with pytest.raises(ValueError, match="oras"):
        be.oras_reference(ENV, {"sif_oras_ref": "ghcr.io/o/x-sif:v1"})


# The manifest must describe the version the declaration asks for.

#: A stale manifest case: declared v0.1.11, manifest resolving to v0.1.10.
LIVE = be.Declared(
    name="croi-general-inference",
    image="ghcr.io/example-org/croi-general-inference:v0.1.11",
    sif_key="environments/example-org/croi-general-inference_v0.1.11.sif",
    declared_by=("pipeline-general-detect",),
)
LIVE_REF = "oras://ghcr.io/example-org/croi-general-inference-sif:v0.1.11"


def _manifest(tag: str, *, oras: bool = True) -> dict:
    out = {
        "image": f"ghcr.io/example-org/croi-general-inference:{tag}",
        "digest": "sha256:" + "b" * 64,
        "source_ref": f"clinical-roi-{tag}",
    }
    if oras:
        out["sif_oras_ref"] = f"oras://ghcr.io/example-org/croi-general-inference-sif:{tag}"
    return out


def test_a_manifest_pinning_an_older_version_is_refused() -> None:
    with pytest.raises(be.StaleManifest) as exc:
        be.oras_reference(LIVE, _manifest("v0.1.10"))
    message = str(exc.value)
    assert "v0.1.11" in message and "v0.1.10" in message
    assert LIVE.sif_key in message


def test_the_refusal_names_the_manifest_directory_and_the_action_that_fixes_it() -> None:
    with pytest.raises(be.StaleManifest) as exc:
        be.oras_reference(LIVE, _manifest("v0.1.10"), "/scratch/x/env-manifests")
    message = str(exc.value)
    assert "/scratch/x/env-manifests" in message
    assert "docs/operations.md section 2.7" in message


def test_a_manifest_that_is_not_a_json_object_names_its_path_and_the_step(tmp_path) -> None:
    (tmp_path / "croi-general-inference.json").write_text('["not", "an", "object"]')
    with pytest.raises(ValueError) as exc:
        be.read_manifest(tmp_path, "croi-general-inference")
    message = str(exc.value)
    assert "croi-general-inference.json" in message
    assert "docs/operations.md section 2.7" in message


def test_a_truncated_manifest_names_its_path_and_the_step(tmp_path) -> None:
    (tmp_path / "croi-general-inference.json").write_text('{"image": "ghcr.io/o/x:v1", "dig')
    with pytest.raises(ValueError) as exc:
        be.read_manifest(tmp_path, "croi-general-inference")
    message = str(exc.value)
    assert "croi-general-inference.json" in message
    assert "docs/operations.md section 2.7" in message


def test_a_malformed_manifest_image_blames_the_manifest_not_the_declaration() -> None:
    with pytest.raises(ValueError) as exc:
        be.oras_reference(
            LIVE,
            {"image": "ghcr.io/example-org/croi-general-inference", "digest": "sha256:" + "b" * 64},
            "/scratch/x/env-manifests",
        )
    message = str(exc.value)
    assert "as the deployment spells it" not in message
    assert "croi-general-inference.json" in message
    assert "/scratch/x/env-manifests" in message
    assert "docs/operations.md section 2.7" in message


def test_a_malformed_digest_names_the_manifest_and_the_step() -> None:
    with pytest.raises(ValueError) as exc:
        be.oras_reference(
            LIVE,
            {"image": LIVE.image, "digest": "sha256:nope", "sif_oras_ref": LIVE_REF},
            "/scratch/x/env-manifests",
        )
    message = str(exc.value)
    assert "croi-general-inference.json" in message
    assert "/scratch/x/env-manifests" in message
    assert "docs/operations.md section 2.7" in message


def test_a_malformed_oras_reference_names_the_manifest_and_the_step() -> None:
    with pytest.raises(ValueError) as exc:
        be.oras_reference(
            LIVE,
            {"image": LIVE.image, "sif_oras_ref": "ghcr.io/o/x-sif:v1"},
            "/scratch/x/env-manifests",
        )
    message = str(exc.value)
    assert "croi-general-inference.json" in message
    assert "/scratch/x/env-manifests" in message
    assert "docs/operations.md section 2.7" in message


def test_a_stale_manifest_is_not_reported_as_an_unpublished_sif() -> None:
    with pytest.raises(be.StaleManifest) as exc:
        be.oras_reference(LIVE, _manifest("v0.1.10", oras=False))
    assert "publishes no ORAS SIF" not in str(exc.value)


def test_a_manifest_at_the_declared_version_resolves_normally() -> None:
    assert be.oras_reference(LIVE, _manifest("v0.1.11")) == LIVE_REF


def test_a_registry_port_is_not_read_as_a_tag() -> None:
    assert be.image_tag("reg:5000/o/x:v1") == "v1"
    with pytest.raises(ValueError, match="<repository>:<tag>"):
        be.image_tag("reg:5000/o/x")


# The flow refuses before it fetches and before it locks.

#: Under the prefect stub `@flow(...)` returns the bare function.
FLOW = getattr(be.build_environments, "fn", be.build_environments)


def _no_run_logger(monkeypatch) -> None:
    """`.fn` runs with no flow run, where real prefect's `get_run_logger` raises."""
    monkeypatch.setattr(be, "get_run_logger", lambda: logging.getLogger("build-environments-test"))


def _one_environment(tmp_path: pathlib.Path, tag: str) -> tuple[pathlib.Path, str]:
    """A storage root and manifest directory with one ``croi-general-inference`` manifest."""
    manifests = tmp_path / "env-manifests"
    manifests.mkdir()
    (manifests / "croi-general-inference.json").write_text(json.dumps(_manifest(tag)))
    return manifests, "croi-general-inference"


def test_a_stale_manifest_stops_the_flow_before_the_download_and_before_the_lock(
    tmp_path, monkeypatch,
) -> None:
    manifests, name = _one_environment(tmp_path, "v0.1.10")
    _no_run_logger(monkeypatch)
    monkeypatch.setattr(be, "fetch_sif", _never_called)

    result = FLOW(storage_root=str(tmp_path), manifest_dir=str(manifests), only=[name])

    row = next(r for r in result["environments"] if r["environment"] == name)
    assert row["state"] == "stale"
    assert "v0.1.10" in row["detail"]
    # Nothing was created at the target path, not even the lock.
    assert list(tmp_path.rglob("*.lock")) == []
    assert not (tmp_path / "environments").exists()


def test_a_matching_manifest_still_reaches_the_fetch(tmp_path, monkeypatch) -> None:
    declared = {e.name: e for e in be.declared_environments()}["croi-general-inference"]
    manifests, name = _one_environment(tmp_path, be.image_tag(declared.image))
    seen: list[str] = []

    def _record(ref, target, **kw):
        seen.append(f"{ref.repository}:{ref.tag}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(BODY)
        return be.Fetched(digest=DIGEST, size=len(BODY))

    _no_run_logger(monkeypatch)
    monkeypatch.setattr(be, "fetch_sif", _record)
    result = FLOW(storage_root=str(tmp_path), manifest_dir=str(manifests), only=[name])

    row = next(r for r in result["environments"] if r["environment"] == name)
    assert row["state"] == "built"
    assert seen == [f"example-org/croi-general-inference-sif:{be.image_tag(declared.image)}"]
    assert (tmp_path / declared.sif_key).exists()


def _never_called(*args, **kwargs):
    raise AssertionError("fetch_sif was called for a manifest that names another version")


@pytest.mark.parametrize("ref,expected", [
    (REF, ("ghcr.io", "example-org/bgnorm-inference-sif", "v0.1.2-cx0.2.6")),
    ("oras://reg:5000/o/x-sif:v1", ("reg:5000", "o/x-sif", "v1")),
])
def test_an_oras_reference_splits_into_registry_repository_and_tag(ref, expected) -> None:
    got = be.parse_oras(ref)
    assert (got.registry, got.repository, got.tag) == expected


def test_a_reference_with_no_tag_is_refused() -> None:
    with pytest.raises(ValueError, match="oras://<registry>"):
        be.parse_oras("oras://ghcr.io/o/x-sif")


# The download.

def _transport(*, body: bytes = BODY, digest: str = DIGEST, size: int | None = None,
               delay: float = 0.0, hits: list | None = None) -> httpx.MockTransport:
    """A registry serving one single-layer SIF artifact."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"token": "t"})
        if "/manifests/" in path:
            return httpx.Response(200, json={"layers": [{
                "mediaType": "application/vnd.sylabs.sif.layer.v1.sif",
                "digest": digest,
                "size": len(body) if size is None else size,
            }]})
        if "/blobs/" in path:
            if hits is not None:
                hits.append(path)

            def stream():
                for i in range(0, len(body), 4):
                    yield body[i:i + 4]
                    if delay:
                        time.sleep(delay)

            return httpx.Response(200, content=stream())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_a_download_lands_at_the_real_path_with_its_digest(tmp_path) -> None:
    target = tmp_path / "environments" / ENV.sif_key.split("/")[-1]
    got = be.fetch_sif(be.parse_oras(REF), target, transport=_transport())
    assert target.read_bytes() == BODY
    assert (got.digest, got.size) == (DIGEST, len(BODY))
    assert target.stat().st_mode & 0o044, "a shared environment has to be group and world readable"


def test_a_present_sif_is_a_no_op(tmp_path) -> None:
    target = tmp_path / "environments" / "e.sif"
    target.parent.mkdir(parents=True)
    target.write_bytes(BODY)
    hits: list = []
    be.fetch_sif(be.parse_oras(REF), target, transport=_transport(hits=hits))
    assert hits == [], "an already-present environment was re-downloaded"
    assert target.read_bytes() == BODY


def test_a_corrupt_transfer_never_reaches_the_real_path(tmp_path) -> None:
    target = tmp_path / "environments" / "e.sif"
    with pytest.raises(RuntimeError, match="hashing to"):
        be.fetch_sif(be.parse_oras(REF), target, transport=_transport(body=b"WRONGWRONGWR"))
    assert not target.exists()
    assert not list(target.parent.glob("*.fetching.*")), "debris would confuse the next builder"


def test_a_short_transfer_is_refused(tmp_path) -> None:
    target = tmp_path / "environments" / "e.sif"
    with pytest.raises(RuntimeError, match="manifest says"):
        be.fetch_sif(be.parse_oras(REF), target, transport=_transport(size=len(BODY) + 1))
    assert not target.exists()


def test_a_multi_layer_artifact_is_refused(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json={"layers": [{"digest": DIGEST, "size": 1},
                                                    {"digest": DIGEST, "size": 2}]})

    with pytest.raises(RuntimeError, match="2 layers"):
        be.fetch_sif(be.parse_oras(REF), tmp_path / "e.sif",
                     transport=httpx.MockTransport(handler))


def test_a_second_builder_on_the_same_path_is_refused_not_queued(tmp_path) -> None:
    import fcntl

    target = tmp_path / "environments" / "e.sif"
    target.parent.mkdir(parents=True)
    lock = target.with_name(target.name + ".lock")
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            be.fetch_sif(be.parse_oras(REF), target, transport=_transport())
    assert not target.exists()


# A transfer the far end drops.

DROPPED = "peer closed connection without sending complete message body"


@pytest.fixture
def pauses(monkeypatch) -> list:
    """Record the backoff instead of sleeping it."""
    slept: list = []
    monkeypatch.setattr(be, "time", type("T", (), {"sleep": staticmethod(slept.append)}))
    return slept


def _flaky(*, drops: int, cut: int = 5, honour_range: bool = True, tail: bytes | None = None,
           ranges: list | None = None) -> httpx.MockTransport:
    """A registry whose first ``drops`` blob transfers die after ``cut`` bytes."""
    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"token": "t"})
        if "/manifests/" in path:
            return httpx.Response(200, json={"layers": [{"digest": DIGEST, "size": len(BODY)}]})
        served["n"] += 1
        wanted = request.headers.get("range")
        if ranges is not None:
            ranges.append(wanted)
        start = int(wanted[len("bytes="):-1]) if wanted and honour_range else 0
        body = BODY[start:] if tail is None or not start else tail
        dropping = served["n"] <= drops

        def stream():
            yield body[:cut] if dropping else body
            if dropping:
                raise httpx.RemoteProtocolError(DROPPED)

        if start:
            return httpx.Response(206, content=stream(), headers={
                "content-range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"})
        return httpx.Response(200, content=stream())

    return httpx.MockTransport(handler)


def test_a_dropped_transfer_over_a_real_socket_is_resumed_not_restarted(tmp_path, pauses) -> None:
    import http.server

    body = bytes(range(256)) * 4096
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    ranges: list = []

    class Registry(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _json(self, doc: dict) -> None:
            raw = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.startswith("/token"):
                return self._json({"token": "t"})
            if "/manifests/" in self.path:
                return self._json({"layers": [{"digest": digest, "size": len(body)}]})
            wanted = self.headers.get("Range")
            ranges.append(wanted)
            if wanted is None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[:len(body) // 3])
                self.wfile.flush()
                self.close_connection = True
                return None
            start = int(wanted[len("bytes="):-1])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            self.wfile.write(body[start:])
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    class Local(httpx.HTTPTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
            return super().handle_request(request)

    target = tmp_path / "environments" / "e.sif"
    try:
        got = be.fetch_sif(be.parse_oras(REF), target, transport=Local())
    finally:
        server.shutdown()
    assert ranges == [None, f"bytes={len(body) // 3}-"], "the retry restarted instead of resuming"
    assert target.read_bytes() == body
    assert got.digest == digest
    assert pauses == [be.FETCH_RETRY_DELAYS[0]]


def test_every_attempt_is_logged_and_the_loop_gives_up(tmp_path, pauses, capsys) -> None:
    target = tmp_path / "environments" / "e.sif"
    attempts = len(be.FETCH_RETRY_DELAYS) + 1
    with pytest.raises(httpx.RemoteProtocolError, match=DROPPED):
        be.fetch_sif(be.parse_oras(REF), target, transport=_flaky(drops=99, cut=1))
    log = capsys.readouterr().out
    for n in range(1, attempts + 1):
        assert f"attempt {n}/{attempts} failed" in log
    assert "giving up" in log
    assert pauses == list(be.FETCH_RETRY_DELAYS)
    assert not target.exists()
    assert not list(target.parent.glob("*.fetching.*"))


def test_a_registry_ignoring_range_restarts_cleanly_and_says_so(tmp_path, pauses, capsys) -> None:
    target = tmp_path / "environments" / "e.sif"
    got = be.fetch_sif(be.parse_oras(REF), target, transport=_flaky(drops=1, honour_range=False))
    assert target.read_bytes() == BODY
    assert got.digest == DIGEST
    assert "restarting the download from zero" in capsys.readouterr().out


def test_a_resumed_file_that_fails_its_digest_is_not_retried(tmp_path, pauses) -> None:
    target = tmp_path / "environments" / "e.sif"
    ranges: list = []
    transport = _flaky(drops=1, tail=b"X" * (len(BODY) - 5), ranges=ranges)
    with pytest.raises(RuntimeError, match="hashing to"):
        be.fetch_sif(be.parse_oras(REF), target, transport=transport)
    assert ranges == [None, "bytes=5-"]
    assert not target.exists()
    assert not list(target.parent.glob("*.fetching.*"))


def test_the_lock_is_held_through_the_backoff(tmp_path, monkeypatch) -> None:
    import fcntl

    target = tmp_path / "environments" / "e.sif"
    refused: list = []

    def probe(_delay) -> None:
        with open(target.with_name(target.name + ".lock"), "w") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                refused.append(True)

    monkeypatch.setattr(be, "time", type("T", (), {"sleep": staticmethod(probe)}))
    be.fetch_sif(be.parse_oras(REF), target, transport=_flaky(drops=2))
    assert refused == [True, True]
    assert target.read_bytes() == BODY


# Eight builders at once, in eight real processes.

_DRIVER = '''
import pathlib, sys, time
sys.path.insert(0, {tests!r})
import httpx
from conftest import load_flow_module

be = load_flow_module("build_environments")
target, hitlog, delay = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
CHUNKS, BODY, DIGEST = {chunks!r}, {body!r}, {digest!r}


def handler(request):
    path = request.url.path
    if path == "/token":
        return httpx.Response(200, json={{"token": "t"}})
    if "/manifests/" in path:
        return httpx.Response(200, json={{"layers": [
            {{"mediaType": "application/vnd.sylabs.sif.layer.v1.sif",
              "digest": DIGEST, "size": len(BODY)}}]}})
    if "/blobs/" in path:
        with open(hitlog, "a") as fh:
            fh.write("hit\\n")

        def stream():
            for chunk in CHUNKS:
                yield chunk
                time.sleep(delay)

        return httpx.Response(200, content=stream())
    return httpx.Response(404)


try:
    got = be.fetch_sif(be.parse_oras({ref!r}), target, transport=httpx.MockTransport(handler))
except BlockingIOError:
    print("LOCKED")
else:
    print("BUILT" if got.digest else "PRESENT", got.size)
'''


def _driver(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "driver.py"
    path.write_text(_DRIVER.format(
        tests=str(TESTS), chunks=CHUNKS, body=BODY, digest=DIGEST, ref=REF,
    ))
    return path


def test_eight_concurrent_builders_produce_one_complete_sif(tmp_path) -> None:
    target = tmp_path / "environments" / ENV.sif_key.split("/")[-1]
    hitlog = tmp_path / "blob-hits"
    driver = _driver(tmp_path)

    seen: list[bytes] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                seen.append(target.read_bytes())
            except OSError:
                pass
            time.sleep(0.002)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: subprocess.run(
                [sys.executable, str(driver), str(target), str(hitlog), "0.3"],
                capture_output=True, text=True, check=False,
            ),
            range(8),
        ))
    stop.set()
    watcher.join(timeout=2)

    for r in results:
        assert r.returncode == 0, f"a builder crashed:\n{r.stdout}\n{r.stderr}"
    outcomes = [r.stdout.strip().split()[0] for r in results]
    assert outcomes.count("BUILT") == 1, f"more than one builder downloaded: {outcomes}"
    assert hitlog.read_text().count("hit") == 1
    assert target.read_bytes() == BODY
    partial = {bytes(s) for s in seen} - {BODY}
    assert not partial, f"the real path held a partial SIF: {sorted(partial)}"
    assert not list(target.parent.glob("*.fetching.*")), "a temp file was left behind"


def test_the_lock_is_released_when_a_builder_is_killed(tmp_path) -> None:
    target = tmp_path / "environments" / "e.sif"
    hitlog = tmp_path / "blob-hits"
    driver = _driver(tmp_path)

    doomed = subprocess.Popen(
        [sys.executable, str(driver), str(target), str(hitlog), "5"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(300):  # wait until it holds the lock and has started
        if hitlog.exists():
            break
        time.sleep(0.02)
    else:
        doomed.kill()
        pytest.fail("the first builder never started its transfer")
    doomed.kill()
    doomed.wait(timeout=10)
    assert not target.exists(), "a killed transfer must leave the real path empty"

    r = subprocess.run([sys.executable, str(driver), str(target), str(hitlog), "0"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert r.stdout.startswith("BUILT")
    assert target.read_bytes() == BODY
    # Two transfers reached the blob: the killed one and the successful one.
    assert hitlog.read_text().count("hit") == 2
