"""Job configuration and variables for SLURM jobs on the HPC."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from prefect.workers.base import BaseJobConfiguration, BaseVariables
from pydantic import Field, field_validator, model_validator

HpcPartition = Literal["general", "gpu_cuda", "gpu_rocm", "gpu_sxm", "gpu_viz"]
HpcQoS = Literal["normal", "debug", "short", "gpu", "mig", "sxm", "sdf", "viz"]
HpcArchConstraint = Literal["epyc3", "epyc4"]

# GPU selector exposed as a job variable. "none" is a CPU-only job.
HpcGpuType = Literal[
    "none",
    "a100",
    "nvidia_a100_80gb_pcie_1g.10gb",
    "nvidia_a100_80gb_pcie_2g.20gb",
    "nvidia_a100_80gb_pcie_3g.40gb",
    "h100",
    "l40",
    "l40s",
    "a16",
    "mi210",
    "mi300x",
]

# GPU type: partitions it is available on.
_GPU_PARTITIONS: dict[str, set[str]] = {
    "a100": {"gpu_cuda"},
    "nvidia_a100_80gb_pcie_1g.10gb": {"gpu_cuda"},
    "nvidia_a100_80gb_pcie_2g.20gb": {"gpu_cuda"},
    "nvidia_a100_80gb_pcie_3g.40gb": {"gpu_cuda"},
    "h100": {"gpu_cuda", "gpu_sxm"},
    "l40": {"gpu_cuda", "gpu_viz"},
    "l40s": {"gpu_cuda", "gpu_viz"},
    "a16": {"gpu_viz"},
    "mi210": {"gpu_rocm"},
    "mi300x": {"gpu_rocm"},
}

# (gpu_type, partition): max CPUs per GPU. Source: the UQ-RCC HPC user guide.
_GPU_MAX_CPUS: dict[tuple[str, str], int] = {
    ("a100", "gpu_cuda"): 86,
    ("nvidia_a100_80gb_pcie_1g.10gb", "gpu_cuda"): 22,
    ("nvidia_a100_80gb_pcie_2g.20gb", "gpu_cuda"): 22,
    ("nvidia_a100_80gb_pcie_3g.40gb", "gpu_cuda"): 22,
    ("h100", "gpu_cuda"): 64,
    ("h100", "gpu_sxm"): 48,
    ("l40", "gpu_cuda"): 64,
    ("l40", "gpu_viz"): 64,
    ("l40s", "gpu_cuda"): 64,
    ("l40s", "gpu_viz"): 64,
    ("a16", "gpu_viz"): 16,
    ("mi210", "gpu_rocm"): 96,
    ("mi300x", "gpu_rocm"): 16,
}

# partition: the QoS values the cluster defines on it, not this account's grants. A value
# here can still be refused at sbatch. Check grants on the HPC with
# `sacctmgr -nP show assoc user=$USER format=account,partition,qos`. --qos is always rendered.
_PARTITION_QOS: dict[str, set[str]] = {
    "general": {"normal", "debug", "short"},
    "gpu_cuda": {"gpu", "debug", "short", "mig"},
    "gpu_rocm": {"gpu", "debug", "short", "sdf"},
    "gpu_sxm": {"sxm"},
    "gpu_viz": {"viz"},
}

# partition: the qos used when a gpu is selected and the current qos is not valid there.
_PARTITION_DEFAULT_QOS: dict[str, str] = {
    "general": "normal",
    "gpu_cuda": "gpu",
    "gpu_rocm": "gpu",
    "gpu_sxm": "sxm",
    "gpu_viz": "viz",
}


def _parse_gres(gres: str) -> tuple[str | None, int]:
    """Parse 'gpu:TYPE:COUNT' or 'gpu:COUNT' into (type_or_none, count)."""
    parts = gres.split(":")
    if len(parts) == 3:
        return parts[1], int(parts[2])
    if len(parts) == 2:
        return None, int(parts[1])
    raise ValueError(f"gres must be 'gpu:TYPE:COUNT' or 'gpu:COUNT', got: {gres!r}")


# SLURM memory suffixes, in kibibytes. A bare number is megabytes to SLURM.
_MEM_KIB: dict[str, int] = {"K": 1, "": 1024, "M": 1024, "G": 1024**2, "T": 1024**3}

_MEM_PATTERN = re.compile(r"(\d+)([KMGT]?)", re.IGNORECASE)


def mem_kib(mem: str) -> int:
    """A SLURM --mem string as kibibytes."""
    m = _MEM_PATTERN.fullmatch(mem.strip())
    if not m:
        raise ValueError(
            f"mem must be a number with optional K/M/G/T suffix, got: {mem!r}"
        )
    return int(m.group(1)) * _MEM_KIB[m.group(2).upper()]


def format_mem(kib: int) -> str:
    """Kibibytes as the largest SLURM suffix that divides them exactly."""
    for suffix in ("T", "G", "M", "K"):
        unit = _MEM_KIB[suffix]
        if kib >= unit and kib % unit == 0:
            return f"{kib // unit}{suffix}"
    return f"{kib}K"


class HpcSlurmJobConfiguration(BaseJobConfiguration):
    job_name: Annotated[
        str | None,
        Field(description="SLURM job name; defaults to flow-<flow run id> if unset"),
    ] = None
    account: Annotated[str, Field(description="Accounting group to charge for the job")]
    partition: Annotated[
        HpcPartition, Field(description="Partition to run the job on")
    ] = "general"
    qos: Annotated[HpcQoS, Field(description="Quality of service to use")] = "normal"
    arch_constraint: Annotated[
        HpcArchConstraint | None, Field(description="Architecture constraint to use")
    ] = None
    nodes: Annotated[int, Field(description="Number of nodes to use")] = 1
    ntasks_per_node: Annotated[int, Field(description="Number of tasks per node")] = 1
    cpus_per_task: Annotated[int, Field(description="Number of CPUs per task")] = 1
    mem: Annotated[str, Field(description="Memory per node")] = "4G"
    oom_retry_max_mem: Annotated[
        str | None,
        Field(
            description="Ceiling for the OOM retry, e.g. 512G. When set, a job SLURM "
            "killed with OUT_OF_MEMORY is resubmitted with DOUBLE this deployment's "
            "mem, clamped to this ceiling, and every doubling names the old and the new "
            "request in the flow run log. A job that OOMs AT the ceiling is not "
            "resubmitted. Unset (the default) means no state is ever retried."
        ),
    ] = None

    walltime: Annotated[str, Field(description="Walltime for the job")] = (
        "1:00:00"  # HH:MM:SS
    )

    # gpu selects the type; derive_gpu_gres derives gres from it. A raw gres may be set when gpu
    # is "none".
    gpu: Annotated[
        HpcGpuType,
        Field(
            description="GPU type to request, or 'none' for a CPU job. When set, "
            "gres is derived and the partition and qos are coerced to a valid "
            "GPU combination."
        ),
    ] = "none"
    gpu_count: Annotated[
        int, Field(description="Number of GPUs of the selected type")
    ] = 1

    gres: Annotated[
        str | None,
        Field(
            description="SLURM gres, e.g. gpu:a100:1. Derived from 'gpu'; a raw "
            "value may be supplied directly."
        ),
    ] = None

    output: Annotated[str | None, Field(description="Output file for the job")] = (
        "slurm-%j.out"
    )
    error: Annotated[str | None, Field(description="Error file for the job")] = (
        "slurm-%j.err"
    )

    array: Annotated[str | None, Field(description="Array job specification")] = None
    export: Annotated[
        str | None, Field(description="Environment variables to export")
    ] = None

    # The image holds the flow code and prefect. The job runs it with apptainer.
    image: Annotated[
        str, Field(description="Container image ref, e.g. ghcr.io/org/pipeline:tag")
    ]
    bind_paths: Annotated[
        list[str], Field(description="Host paths bind-mounted into the container")
    ]
    apptainer_cachedir: Annotated[
        str | None,
        Field(
            description="APPTAINER_CACHEDIR for pulled images; a shared scratch "
            "path reuses images across jobs instead of re-pulling each run"
        ),
    ] = None
    registry_auth_block: Annotated[
        str | None,
        Field(
            description="Name of a saved ContainerRegistryAuth block; when set, "
            "the worker exports its credentials before the apptainer pull so a "
            "private image (e.g. ghcr) can be fetched"
        ),
    ] = None
    sif_artifact: Annotated[
        str | None,
        Field(
            description="lamindb Artifact key OR uid of the prebuilt SIF (kind=environment). "
            "When set, the worker resolves it to the SIF .path and execs that; if the path "
            "is missing on the HPC it apptainer-pulls from the artifact's docker_ref feature "
            "to build it there. Takes precedence over `image`."
        ),
    ] = None
    run_on_login_node: Annotated[
        bool,
        Field(
            description="Run this flow directly on the HPC login node (no sbatch, "
            "no apptainer - which is compute-node only). For lightweight flows that "
            "just poll/query and need no compute allocation (e.g. a health check)."
        ),
    ] = False
    login_python: Annotated[
        str | None,
        Field(
            description="Path to an HPC login-node prefect executable (or venv bin) "
            "used when run_on_login_node; its directory is prepended to PATH so the "
            "command resolves. Required for login-node flows."
        ),
    ] = None

    def prepare_for_flow_run(self, flow_run, *args, **kwargs) -> None:
        """Populate command, env and name, and default job_name to flow-<flow run id>."""
        super().prepare_for_flow_run(flow_run, *args, **kwargs)
        if not self.job_name:
            self.job_name = f"flow-{flow_run.id}"

    @field_validator("account")
    @classmethod
    def account_prefix(cls, v: str) -> str:
        if not v.startswith("a_"):
            raise ValueError(f"HPC account must start wih 'a_', got: {v!r}")
        return v

    @field_validator("mem", "oom_retry_max_mem")
    @classmethod
    def mem_format(cls, v: str | None) -> str | None:
        if v is None:  # only oom_retry_max_mem is optional; mem has a default
            return v
        mem_kib(v)  # raises on a malformed value
        return v.upper()

    @field_validator("walltime")
    @classmethod
    def walltime_format(cls, v: str) -> str:
        if not re.fullmatch(r"(?:\d+-)?(?:\d+):\d{2}:\d{2}", v):
            raise ValueError(f"walltime must be [D-]HH:MM:SS, got: {v!r}")
        return v

    @field_validator("gres")
    @classmethod
    def gres_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("gpu:"):
            raise ValueError(f"gres must start with 'gpu:', got: {v!r}")
        _parse_gres(v)
        return v

    @field_validator("array")
    @classmethod
    def array_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        # TODO: validation as per https://slurm.schedmd.com/job_array.html
        return v

    @model_validator(mode="after")
    def derive_gpu_gres(self) -> "HpcSlurmJobConfiguration":
        """Turn gpu into a SLURM gres string; coerce partition and qos to a valid GPU combination.

        A gpu of 'none' leaves gres untouched.
        """
        if self.gpu != "none":
            self.gres = f"gpu:{self.gpu}:{self.gpu_count}"
            if self.partition == "general":
                self.partition = sorted(_GPU_PARTITIONS[self.gpu])[0]
            if self.qos not in _PARTITION_QOS.get(self.partition, set()):
                self.qos = _PARTITION_DEFAULT_QOS[self.partition]
        return self

    @model_validator(mode="after")
    def oom_ceiling_covers_the_request(self) -> "HpcSlurmJobConfiguration":
        """Refuse a ceiling below the mem request. A ceiling equal to the request is allowed."""
        ceiling = self.oom_retry_max_mem
        if ceiling and mem_kib(ceiling) < mem_kib(self.mem):
            raise ValueError(
                f"oom_retry_max_mem={ceiling!r} is below mem={self.mem!r}; "
                f"a ceiling must cover the request it is a ceiling for"
            )
        return self

    @model_validator(mode="after")
    def gpu_resource_needs_gpu_partition(self) -> "HpcSlurmJobConfiguration":
        if self.gres and self.partition == "general":
            raise ValueError(
                f"gres={self.gres!r} requires a GPU partition "
                f"(gpu_cuda, gpu_rocm, gpu_sxm, gpu_viz), not 'general'"
            )
        return self

    @model_validator(mode="after")
    def gpu_partition_needs_gres(self) -> "HpcSlurmJobConfiguration":
        if self.partition != "general" and not self.gres:
            raise ValueError(
                f"partition={self.partition!r} requires a GPU: set 'gpu' or a raw 'gres'"
            )
        return self

    @model_validator(mode="after")
    def gpu_partition_gres_combination(self) -> "HpcSlurmJobConfiguration":
        if not self.gres:
            return self

        gpu_type, gpu_count = _parse_gres(self.gres)
        if gpu_type is None:
            return self

        if gpu_type not in _GPU_PARTITIONS:
            raise ValueError(
                f"Unknown GPU type {gpu_type!r}. Valid types: {sorted(_GPU_PARTITIONS)}"
            )

        valid_partitions = _GPU_PARTITIONS[gpu_type]
        if self.partition not in valid_partitions:
            raise ValueError(
                f"GPU type {gpu_type!r} is not available on partition={self.partition!r}. "
                f"Valid partitions for this GPU: {sorted(valid_partitions)}"
            )

        max_cpus = _GPU_MAX_CPUS.get((gpu_type, self.partition))
        if max_cpus is not None:
            total_cpus = self.cpus_per_task * self.ntasks_per_node * self.nodes
            limit = max_cpus * gpu_count
            if total_cpus > limit:
                raise ValueError(
                    f"{gpu_type!r} on {self.partition!r} allows max {max_cpus} CPUs per GPU "
                    f"({gpu_count} GPU(s): limit {limit} CPUs). "
                    f"Requested: {total_cpus} CPUs "
                    f"({self.cpus_per_task} cpus_per_task x {self.ntasks_per_node} tasks "
                    f"x {self.nodes} nodes)."
                )

        return self

    @model_validator(mode="after")
    def partition_qos_combination(self) -> "HpcSlurmJobConfiguration":
        valid_qos = _PARTITION_QOS.get(self.partition, set())
        if self.qos not in valid_qos:
            raise ValueError(
                f"qos={self.qos!r} is not valid for partition={self.partition!r}. "
                f"Valid QoS values: {sorted(valid_qos)}"
            )
        return self


class HpcSlurmVariables(BaseVariables):
    """User-facing job variables for the hpc-slurm work pool.

    Defaults target the general CPU partition. Setting gpu is enough for a GPU job. Names mirror
    HpcSlurmJobConfiguration. gres, output, error, array, export and apptainer_cachedir are
    advanced fields a deployment normally leaves unset.
    """

    job_name: Annotated[
        str | None,
        Field(description="SLURM job name; defaults to flow-<flow run id> if unset"),
    ] = None
    image: Annotated[
        str, Field(description="Container image ref, e.g. ghcr.io/org/pipeline:tag")
    ]
    # Site values with no default here: deploy.py fills them from site.env
    # (CLINICALOMX_SITE_CONTAINER_BINDS, _SLURM_ACCOUNT, _SLURM_PARTITION, _SLURM_QOS).
    bind_paths: Annotated[
        list[str], Field(description="Host paths bind-mounted into the container")
    ]
    account: Annotated[
        str, Field(description="Accounting group; must start with a_")
    ]
    partition: Annotated[
        HpcPartition,
        Field(description="Partition; usually left general and derived from gpu"),
    ]
    qos: Annotated[
        HpcQoS,
        Field(
            description=(
                "Quality of service; derived from partition when a gpu is set. The worker "
                "always sends --qos, and the choices are the cluster's catalogue, not this "
                "account's grants: a QoS the account lacks (e.g. mig) is refused at sbatch"
            )
        ),
    ]
    gpu: Annotated[
        HpcGpuType, Field(description="GPU type to request, or 'none' for a CPU job")
    ] = "none"
    gpu_count: Annotated[
        int, Field(description="Number of GPUs of the selected type")
    ] = 1
    cpus_per_task: Annotated[
        int,
        Field(
            description="CPUs per task; flows read SLURM_CPUS_PER_TASK to size "
            "their own parallelism"
        ),
    ] = 1
    mem: Annotated[str, Field(description="Memory per node, e.g. 64G, 256G")] = "4G"
    oom_retry_max_mem: Annotated[
        str | None,
        Field(
            description="Ceiling for the OOM retry, e.g. 512G. Set it and a job SLURM "
            "killed with OUT_OF_MEMORY is resubmitted with double the memory, up to this "
            "value; leave it unset and no failure is ever retried. Opt in per deployment, "
            "not pool-wide: only a builder whose peak genuinely varies with its input "
            "earns a resubmission."
        ),
    ] = None
    walltime: Annotated[str, Field(description="Walltime, HH:MM:SS or D-HH:MM:SS")] = (
        "1:00:00"
    )
    nodes: Annotated[int, Field(description="Number of nodes")] = 1
    ntasks_per_node: Annotated[int, Field(description="Tasks per node")] = 1
    arch_constraint: Annotated[
        HpcArchConstraint | None, Field(description="CPU architecture constraint")
    ] = None

    # Advanced fields. Prefect requires every template variable to be declared.
    gres: Annotated[
        str | None,
        Field(description="SLURM gres, e.g. gpu:a100:1; normally derived from gpu"),
    ] = None
    output: Annotated[str | None, Field(description="Output file for the job")] = (
        "slurm-%j.out"
    )
    error: Annotated[str | None, Field(description="Error file for the job")] = (
        "slurm-%j.err"
    )
    array: Annotated[str | None, Field(description="Array job specification")] = None
    export: Annotated[
        str | None, Field(description="Environment variables to export")
    ] = None
    apptainer_cachedir: Annotated[
        str | None,
        Field(description="APPTAINER_CACHEDIR for pulled images; shared scratch path"),
    ] = None
    registry_auth_block: Annotated[
        str | None,
        Field(
            description="Name of a saved ContainerRegistryAuth block for pulling a "
            "private image; leave unset for public images"
        ),
    ] = None
    sif_artifact: Annotated[
        str | None,
        Field(
            description="lamindb Artifact key/uid of the prebuilt SIF; the worker execs "
            "its .path (pulling from its docker_ref feature if missing). Overrides image."
        ),
    ] = None
    run_on_login_node: Annotated[
        bool,
        Field(
            description="Run on the HPC login node (no sbatch/apptainer) - for "
            "lightweight polling flows that need no compute allocation"
        ),
    ] = False
    login_python: Annotated[
        str | None,
        Field(
            description="Path to a login-node prefect executable/venv bin, used when "
            "run_on_login_node (its dir is prepended to PATH)"
        ),
    ] = None
