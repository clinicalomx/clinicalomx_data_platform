"""render.py refuses a site file it cannot vouch for, and renders one it can, byte for byte.

    uv run --no-project --with pydantic --with pyyaml --with pytest pytest scripts/site -q
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("site_render", HERE / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

# Documentation-range and synthetic values only: no real site fact belongs in this tree.
SITE = {
    "schema_version": 1,
    "hub": {"public_address": "203.0.113.10", "compute_ingress_cidr": "198.51.100.7/32",
            "portal_url": "http://localhost:8080"},
    "lamin": {"instance": "owner/name"},
    "storage": {"hpc_root": "/hpc/project_mnt/P1/dbstorage", "hpc_alias": "/hpc/project/p/dbstorage",
                "raw_root": "/rdm/Q1", "raw_scratch_root": "/rdm/Q2",
                "model_root": "/hpc/project_mnt/P1/dbstorage/models",
                "container_binds": ["/hpc", "/rdm"]},
    "hpc": {"login_host": "login.example.edu", "slurm_account": "a_project",
            "default_partition": "general", "default_qos": "normal",
            "user_scratch_root": "/hpc/users", "users": ["alice", "bob"],
            "nat_address": "198.51.100.7", "compute_subnet": "10.10.0.0/23"},
    "registry": {"images": "ghcr.io/example-org"},
    "packages": {"index_url": "https://objects.example.edu/index/simple/"},
    "source": {"repository": "example-org/clinicalomx_data_platform"},
}


def _load(tmp_path, data):
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(data))
    return render.load(path)


def _with(**changes):
    data = yaml.safe_load(yaml.safe_dump(SITE))
    for dotted, value in changes.items():
        *parents, leaf = dotted.split("__")
        node = data
        for part in parents:
            node = node[part]
        if value is None:
            del node[leaf]
        else:
            node[leaf] = value
    return data


def test_a_complete_site_renders_both_files(tmp_path):
    site = _load(tmp_path, SITE)
    env = render.render_env(site)
    assert "PREFECT_EXTERNAL_API_URL=http://203.0.113.10:4200/api\n" in env
    assert "MLFLOW_EXTERNAL_TRACKING_URI=http://203.0.113.10:5000\n" in env
    assert "CLINICALOMX_SITE_HPC_USER=alice\n" in env
    assert "CLINICALOMX_SITE_IMAGE_OWNER=" + SITE["registry"]["images"].rsplit("/", 1)[-1] + "\n" in env
    assert "CLINICALOMX_SITE_HPC_NAT_ADDRESS=198.51.100.7\n" in env
    assert "CLINICALOMX_SITE_HPC_COMPUTE_SUBNET=10.10.0.0/23\n" in env
    assert render.render_users(site).splitlines()[-2:] == ["alice", "bob"]


def test_the_release_variables_come_from_the_index_url(tmp_path):
    assert render.github_variables(_load(tmp_path, SITE)) == {
        "UV_S3_ENDPOINT_URL": "https://objects.example.edu/", "INDEX_BUCKET": "index"}


def test_no_index_leaves_the_release_variables_empty(tmp_path):
    site = _load(tmp_path, {**SITE, "packages": {"index_url": None}})
    assert render.github_variables(site) == {"UV_S3_ENDPOINT_URL": "", "INDEX_BUCKET": ""}


@pytest.mark.parametrize("changes, said", [
    ({"hub__public_address": "<203.0.113.10>"}, "placeholder"),
    ({"hpc__users": ["<username>"]}, "placeholder"),
    ({"registry__images": "ghp_" + "a" * 36}, "GitHub token"),
    ({"packages__index_url": "https://user:secret@objects.example.edu/simple/"}, "credential"),  # pragma: allowlist secret - synthetic RFC 2606 value proving the refusal
    ({"lamin__instance": "f" * 40}, "hex"),
    ({"hub__webhook": "https://x.webhook.office.com/abc"}, "webhook"),
    ({"hub__extra_key": "x"}, "Extra inputs"),
    ({"hpc__slurm_account": None}, "required"),
    ({"storage__model_root": "/home/someone/models"}, "container_binds"),
    ({"storage__raw_root": "relative/path"}, "absolute"),
    ({"hpc__user_scratch_root": "/hpc/users/"}, "absolute"),
    ({"hpc__users": ["alice", "alice"]}, "twice"),
    ({"hpc__nat_address": "<198.51.100.7>"}, "placeholder"),
    ({"hpc__nat_address": "198.51.100.0/24"}, "IPv4 or IPv6 address"),
    ({"hpc__nat_address": "198.51.100.8"}, "outside hub.compute_ingress_cidr"),
    ({"hpc__compute_subnet": "0.0.0.0/0"}, "private IPv4 network"),
    ({"hpc__compute_subnet": "10.10.0.1/23"}, "host bits set"),
    ({"schema_version": 2}, "schema_version"),
])
def test_a_bad_site_file_is_refused(tmp_path, changes, said):
    with pytest.raises(Exception, match=said):
        _load(tmp_path, _with(**changes))


def test_a_refusal_never_prints_the_value(tmp_path, capsys):
    token = "ghp_" + "b" * 36
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(_with(registry__images=token)))
    assert render.main(["--check", str(path)]) == 1
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert "registry.images" in out.err


def test_the_example_has_the_model_shape():
    site = render.load(HERE.parents[1] / "site.example.yaml", example=True)
    assert render.environment(site)
