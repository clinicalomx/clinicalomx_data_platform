"""Stage 2 config-level reproduction: systemd units parse; prefect.yaml deployments
match the live-systems inventory (name, pool, schedule, image ref). Read-only."""
import pathlib
import re
import sys

import yaml

ROOT = str(pathlib.Path(__file__).resolve().parents[2])
problems = []

# --- systemd units: INI-shaped, [Unit]+[Service]/[Timer], an Exec* in every service ---
UNITS = [
    "packages/clinicalomx_pipelines/qptiff_pipeline/roi-review-app.service",
    "packages/clinicalomxdb/hub-ops/health/ops-health.service",
    "packages/clinicalomxdb/hub-ops/login/ops-login.service",
    "packages/clinicalomxdb/hub-ops/schema-review/ops-schema-review.service",
    "packages/clinicalomxdb/hub-ops/ttyd/ops-ttyd.service",
    "packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/hpc-ssh-pool-worker.service",
    "packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/hpc-ssh-pool-worker@.service",
    "packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-pipeline/pipeline-pool-worker.service",
    "packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/vm-pool-worker.service",
]
section_re = re.compile(r"^\[(\w+)\]$")
kv_re = re.compile(r"^[A-Za-z][A-Za-z0-9]*=")
for path in UNITS:
    sections, current, exec_found, home_override = [], None, False, False
    joined, buf = [], ""
    for i, raw in enumerate(open(f"{ROOT}/{path}"), 1):
        raw = raw.rstrip("\n")
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        joined.append((i, buf + raw))
        buf = ""
    for i, line in joined:
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = section_re.match(line)
        if m:
            current = m.group(1)
            sections.append(current)
            continue
        if current is None or not kv_re.match(line):
            problems.append(f"{path}:{i}: not a section header or Key=Value: {line[:60]}")
            continue
        if current == "Service" and line.startswith(("ExecStart=", "ExecStartPre=")):
            exec_found = True
        if line.startswith("Environment=") and "HOME=" in line:
            home_override = True
    if "Unit" not in sections or "Service" not in sections:
        problems.append(f"{path}: missing [Unit] or [Service]")
    if not exec_found:
        problems.append(f"{path}: no ExecStart")
    tokens = sorted(set(re.findall(r"@[A-Z_]+@", open(f"{ROOT}/{path}").read())))
    print(f"unit OK: {path}  sections={sections}  home_override={home_override}"
          + (f"  template_tokens={tokens}" if tokens else ""))

# --- cloud-init-written units present by name in the tftpl ---
tftpl = open(f"{ROOT}/infra/control_plane/cloud_init.yaml.tftpl").read()
for name in ["clinicalomxdb.service", "pg-backup.service", "pg-backup.timer", "pg-backup-alert.service"]:
    ok = name in tftpl
    print(f"cloud-init defines {name}: {ok}")
    if not ok:
        problems.append(f"cloud_init.yaml.tftpl missing {name}")

# --- prefect.yaml vs the deployments declared here ---
EXPECTED_PIPELINE = {  # name: (pool, interval-or-None, image-substr-or-None)
    "pipeline-register-qptiffs": ("hpc-ssh-pool", 259300, "comx-qptiff:cx0.1.0-bio23e46aa"),
    "pipeline-check-qptiffs": ("hpc-ssh-pool", None, "comx-qptiff:cx0.1.0-bio23e46aa"),
    "pipeline-general-detect": ("hpc-ssh-pool", None, "croi-general-inference:v0.3.5"),
    "pipeline-general-persist": ("hpc-ssh-pool", None, "croi-general-inference:v0.3.5"),
    "pipeline-core-detect": ("hpc-ssh-pool", None, "croi-core-inference:v0.3.5"),
    "pipeline-core-persist": ("hpc-ssh-pool", None, "croi-core-inference:v0.3.5"),
    "pipeline-bgnorm-core": ("hpc-ssh-pool", None, "bgnorm-inference:v0.1.2-cx0.1.0"),
    "pipeline-bgnorm-runner": ("vm-pool", None, None),
    # Listed whether or not it is live yet, because this check reads prefect.yaml and an
    # undeclared deployment is reported as an extra.
    "pipeline-extract-assemble": ("hpc-ssh-pool", None, "comx-extract:cx0.1.0-bio23e46aa"),
    "pipeline-reset-slide": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-heal-orphaned-bgnorm": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-clear-bgnorm-failures": ("vm-pool", None, None),
    "utils-sweep-orphan-bgnorm-zarrs": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-reset-bgnorm": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-reset-tma-cores": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-audit-storage": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-cleanup-storage": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-reconcile-bytes": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-delete-byteless-artifacts": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-reset-all-roi": ("hpc-ssh-pool", None, "comx-qptiff"),
    "pipeline-data-tests": ("hpc-ssh-pool", 604800, "croi-core-inference:v0.3.5"),
    "pipeline-data-tests-vm": ("vm-pool", 86400, None),
    "utils-restore-trashed-artifacts": ("hpc-ssh-pool", None, "comx-qptiff"),
    "utils-build-environments": ("hpc-ssh-pool", None, "comx-qptiff"),
    "pipeline-harvest-feedback": ("vm-pool", 86400, None),
    "pipeline-mint-slides": ("vm-pool", None, None),
    "pipeline-check-general-detect": ("vm-pool", 86400, None),
    "pipeline-ensure-qptiffs-checked": ("vm-pool", 259200, None),
    "pipeline-sync-model-aliases": ("vm-pool", 86400, None),
}
EXPECTED_DETECTOR = {
    "train-general": ("hpc-ssh-pool", None, "croi-general-train:v0.3.5"),
    "train-core": ("hpc-ssh-pool", None, "croi-core-train:v0.3.5"),
    "utils-recover-annotated-images": ("hpc-ssh-pool", None, "croi-core-train:v0.3.5"),
    "ingest-labels-core": ("hpc-ssh-pool", None, "croi-core-train:v0.3.5"),
    "split-core": ("hpc-ssh-pool", None, "croi-core-train:v0.3.5"),
}


SITE_REGISTRY = "{{ $CLINICALOMX_SITE_IMAGE_REGISTRY }}"
SITE_OWNER = "{{ $CLINICALOMX_SITE_IMAGE_OWNER }}"


def check_prefect(path, expected, project_name):
    """Compare one prefect.yaml against its expected inventory, appending to `problems`.

    Checks the project name, then every deployment's pool, schedule interval and image
    against `expected`, and refuses a deployment the inventory does not list. Also checks
    that each `sif_artifact` names the same environment as its `image`, because the worker
    derives the image from that key when the artifact is missing, so a disagreement runs
    one image while the provenance record names another.
    """
    doc = yaml.safe_load(open(f"{ROOT}/{path}"))
    if doc.get("name") != project_name:
        problems.append(f"{path}: project name {doc.get('name')!r} != {project_name!r}")
    seen = {}
    for d in doc["deployments"]:
        pool = d.get("work_pool", {}).get("name")
        jv = d.get("work_pool", {}).get("job_variables", {}) or {}
        image = jv.get("image")
        schedules = d.get("schedules") or []
        interval = schedules[0].get("interval") if schedules else None
        seen[d["name"]] = (pool, interval, image)
        # image and sif_artifact must name the SAME environment. The worker resolves the artifact
        # by key and, when it is missing, derives the image FROM that key, so a disagreement means
        # a run executes one image while its provenance record names another. Same convention as
        # clinicalomx.environments.sif_key_for_image, asserted here from the definition side.
        sif = jv.get("sif_artifact")
        if sif and image:
            # Both spell the site's registry as a template; render them alike before comparing.
            image, sif = (v.replace(SITE_REGISTRY, "ghcr.io/owner").replace(SITE_OWNER, "owner")
                          for v in (image, sif))
            repository, tag = image.rsplit(":", 1)
            owner, name = repository.split("/")[-2:]
            want = f"environments/{owner}/{name}_{tag}.sif"
            if sif != want:
                problems.append(f"{path}:{d['name']}: sif_artifact {sif} does not match "
                                f"image {image} (expected {want})")
        elif sif and not image:
            problems.append(f"{path}:{d['name']}: sif_artifact {sif} with no image to bind it to")
    for name, (pool, interval, image_sub) in expected.items():
        if name not in seen:
            problems.append(f"{path}: expected deployment {name} missing")
            continue
        got_pool, got_interval, got_image = seen[name]
        if got_pool != pool:
            problems.append(f"{path}:{name}: pool {got_pool} != {pool}")
        if got_interval != interval:
            problems.append(f"{path}:{name}: interval {got_interval} != {interval}")
        if image_sub and (not got_image or image_sub not in got_image):
            problems.append(f"{path}:{name}: image {got_image} lacks {image_sub}")
    extra = set(seen) - set(expected)
    if extra:
        problems.append(f"{path}: deployments not in inventory: {sorted(extra)}")
    print(f"{path}: {len(seen)} deployments, all match inventory: {not problems}")
    # pull mechanism
    print(f"{path}: pull steps: {doc.get('pull')}")


check_prefect("packages/clinicalomx_pipelines/qptiff_pipeline/prefect.yaml", EXPECTED_PIPELINE, "clinicalomx-pipeline")
check_prefect("packages/clinicalomx_ml/roi_detection/prefect.yaml", EXPECTED_DETECTOR,
              "clinical-image-roi-object-detection")

print("\nPROBLEMS:" if problems else "\nALL CHECKS PASS")
for p in problems:
    print(" -", p)
sys.exit(1 if problems else 0)
