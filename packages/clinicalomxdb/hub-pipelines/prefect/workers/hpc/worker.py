"""Prefect worker that submits and monitors SLURM jobs on the HPC over SSH.

For each flow run it renders an sbatch script, submits it, and polls squeue and sacct until
the job ends. The flow reports its own state; the worker reports the job result.

A job killed with OUT_OF_MEMORY is resubmitted with double the memory when the deployment
sets oom_retry_max_mem. No other failure is retried.
"""

from __future__ import annotations

import os
import shlex
import time

import anyio
import anyio.abc
from config import (
    HpcSlurmJobConfiguration,
    HpcSlurmVariables,
    format_mem,
    mem_kib,
)
from connection import ContainerRegistryAuth, SSHConnection
from prefect.client.schemas.objects import FlowRun
from prefect.logging.loggers import get_logger
from prefect.states import Cancelled
from prefect.workers.base import BaseWorker, BaseWorkerResult

_SUCCESS_STATES = {"COMPLETED"}

# The only sacct state that earns a resubmission.
_OOM_STATE = "OUT_OF_MEMORY"

# sbatch refusals decided from the request alone. These are never retried. Transient
# controller errors and QOS limit refusals are absent, so they keep the retry.
_SLURM_REFUSALS = (
    "Invalid qos specification",
    "Invalid account or account/partition combination specified",
    "Invalid partition name specified",
)


class SubmissionRefused(RuntimeError):
    """SLURM refused the job script itself; no job was queued and a retry cannot help."""


def _refusal(exc: Exception, configuration: HpcSlurmJobConfiguration) -> str | None:
    """The operator-facing reason when `exc` is one of _SLURM_REFUSALS, else None."""
    reason = next((r for r in _SLURM_REFUSALS if r in str(exc)), None)
    if reason is None:
        return None
    c = configuration
    asked = f"account={c.account!r} partition={c.partition!r} qos={c.qos!r}"
    msg = f"SLURM refused the job ({reason}) for {asked}; no job was queued."
    if reason == "Invalid qos specification":
        # config._PARTITION_QOS lists the cluster's QoS values, not this account's grants.
        msg += (
            f" The account does not hold qos={c.qos!r} on {c.partition!r}, although the "
            f"cluster defines it. Set qos to one the account holds (qos='gpu' is "
            f"confirmed on gpu_cuda); `sacctmgr -nP show assoc user=$USER "
            f"format=account,partition,qos` on the HPC lists them."
        )
    return msg


def _next_mem_request(current: str, ceiling: str) -> str | None:
    """Double `current`, clamped to `ceiling`. None when no larger request is possible.

    `--mem=0` means all of a node's memory and returns None.
    """
    now, top = mem_kib(current), mem_kib(ceiling)
    if now <= 0 or now >= top:
        return None
    return format_mem(min(now * 2, top))


def _external_mlflow_uri() -> str | None:
    """The MLflow address an HPC job uses: MLFLOW_EXTERNAL_TRACKING_URI, else MLFLOW_TRACKING_URI.

    The worker's own MLFLOW_TRACKING_URI is local; jobs need the external address.
    """
    external = os.environ.get("MLFLOW_EXTERNAL_TRACKING_URI")
    if external:
        return external
    legacy = os.environ.get("MLFLOW_TRACKING_URI")
    if legacy and "localhost" not in legacy and "127.0.0.1" not in legacy:
        return legacy
    return None


def _site_exports() -> list[str]:
    """Export lines passing every CLINICALOMX_SITE_* variable and LAMIN_INSTANCE into a job."""
    return [f"export {key}={shlex.quote(value)}" for key, value in sorted(os.environ.items())
            if key.startswith("CLINICALOMX_SITE_") or key == "LAMIN_INSTANCE"]


class HpcSlurmSSHPushWorkerResult(BaseWorkerResult):
    # The final sacct state.
    slurm_state: str | None = None


class HpcSlurmSSHPushWorker(
    BaseWorker[
        HpcSlurmJobConfiguration,
        HpcSlurmVariables,
        HpcSlurmSSHPushWorkerResult,
    ]
):
    type = "hpc-slurm-ssh-pusher"
    job_configuration = HpcSlurmJobConfiguration
    job_configuration_variables = HpcSlurmVariables
    _description = (
        "Push-based worker which submits and monitors SLURM jobs on the HPC "
        "via an SSH connection."
    )

    # SSHConnection block named "<host>-<user>" (see deploy.py), or HPC_SSH_BLOCK when set.
    @property
    def ssh_block_name(self) -> str:
        return os.environ.get("HPC_SSH_BLOCK") or f"hpc-{os.environ['CLINICALOMX_SITE_HPC_USER']}"

    # Seconds. Polling is fast after submission and slower once the job runs.
    startup_timeout: int = 300
    poll_interval_pending: int = 10
    poll_interval_running: int = 30
    max_poll_failures: int = 5
    max_submit_attempts: int = 4
    submit_retry_delay: int = 15
    # Max seconds a login-node flow may run.
    login_timeout: int = 600

    # Shared across concurrent runs; the command methods hold no per-run state.
    _connection: SSHConnection | None = None

    async def _get_connection(self) -> SSHConnection:
        if self._connection is None:
            self._connection = await SSHConnection.connect(self.ssh_block_name)
        return self._connection

    async def _ssh(
        self,
        command: str,
        stdin: str | None = None,
        timeout: int = 30,
        attempts: int = 1,
        retry_delay: float = 5.0,
    ):
        """Run a SLURM command on the login node, in a thread."""
        conn = await self._get_connection()
        return await anyio.to_thread.run_sync(
            lambda: conn.run(
                command,
                timeout=timeout,
                stdin=stdin,
                attempts=attempts,
                retry_delay=retry_delay,
            )
        )

    async def _sbatch(self, script: str) -> str:
        """Submit a job script via sbatch stdin and return the job id. One attempt only.

        Retry only through _submit_with_retry, which checks for an already-queued job.
        """
        result = await self._ssh("sbatch", stdin=script)
        return self._parse_sbatch_job_id(result.stdout)

    async def _job_id_by_name(self, job_name: str) -> str | None:
        """The id of a queued or running job with this name, or None. Retries internally.

        Job names are unique per flow run (flow-<flow run id>).
        """
        result = await self._ssh(
            f"squeue -h -n {shlex.quote(job_name)} -o %A", attempts=2, retry_delay=5
        )
        out = result.stdout.strip()
        return out.splitlines()[0].strip() if out else None

    async def _submit_with_retry(
        self,
        script: str,
        job_name: str,
        logger,
        configuration: HpcSlurmJobConfiguration,
    ) -> str:
        """sbatch with bounded retries. Never submits twice for one flow run.

        Before each retry, a queued job with this run's name is adopted instead of resubmitting.
        A refusal in _SLURM_REFUSALS raises SubmissionRefused on the first attempt.
        """
        last: Exception | None = None
        for attempt in range(1, self.max_submit_attempts + 1):
            if attempt > 1:
                try:
                    existing = await self._job_id_by_name(job_name)
                except Exception as exc:  # cannot check, so do not risk a duplicate
                    logger.warning(
                        f"could not check for an existing job named {job_name!r} "
                        f"before retrying sbatch: {exc}"
                    )
                    existing = None
                if existing:
                    logger.warning(
                        f"sbatch attempt {attempt - 1} did land after all; adopting "
                        f"already-queued job {existing} for {job_name!r}"
                    )
                    return existing
            try:
                return await self._sbatch(script)
            except Exception as exc:
                refused = _refusal(exc, configuration)
                if refused:
                    logger.error(refused)
                    raise SubmissionRefused(refused) from exc
                last = exc
                if attempt >= self.max_submit_attempts:
                    break
                logger.warning(
                    f"sbatch failed for {job_name!r} "
                    f"(attempt {attempt}/{self.max_submit_attempts}), retrying in "
                    f"{self.submit_retry_delay}s: {exc}"
                )
                await anyio.sleep(self.submit_retry_delay)
        logger.error(
            f"sbatch failed for {job_name!r} after {self.max_submit_attempts} attempt(s); "
            f"the flow run never started. If the SSH ControlMaster is dead, re-auth with "
            f"`ssh {getattr(self._connection, 'host', 'hpc')} true`."
        )
        raise last

    async def _squeue_state(self, job_id: str) -> str | None:
        """The live job state from squeue, or None when the job is not in the queue."""
        result = await self._ssh(f"squeue -j {job_id} -h -o %T")
        out = result.stdout.strip()
        return out.splitlines()[0].strip() if out else None

    async def _sacct_state(self, job_id: str) -> str:
        """The terminal state from sacct's job-level row."""
        result = await self._ssh(
            f"sacct -j {job_id} --format=State --noheader --parsable2"
        )
        return self._parse_sacct_state(result.stdout)

    async def _scancel(self, job_id: str) -> None:
        await self._ssh(f"scancel {job_id}")

    @staticmethod
    def _parse_sbatch_job_id(stdout: str) -> str:
        # sbatch prints "Submitted batch job 12345"
        parts = stdout.strip().split()
        if not parts or not parts[-1].split("_")[0].isdigit():
            raise RuntimeError(f"could not parse job id from sbatch output: {stdout!r}")
        return parts[-1]

    @staticmethod
    def _parse_sacct_state(stdout: str) -> str:
        # One row per job step; the first is the job-level row.
        lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
        return lines[0] if lines else ""

    async def _registry_env(
        self, configuration: HpcSlurmJobConfiguration
    ) -> dict[str, str]:
        """The apptainer pull credentials from the deployment's ContainerRegistryAuth block.

        Empty when no block is named. The secret lives only in the sbatch script sent over SSH,
        never in the deployment definition.
        """
        name = configuration.registry_auth_block
        if not name:
            return {}
        auth = await ContainerRegistryAuth.aload(name)
        return auth.apptainer_env()

    async def _resolve_sif(
        self, configuration: HpcSlurmJobConfiguration
    ) -> tuple[str, str | None] | None:
        """Resolve the deployment's SIF artifact (key or uid) to (sif_path, docker_ref).

        None when no sif_artifact is set. A missing artifact gets a path under
        CLINICALOMX_SITE_HPC_ALIAS and is built in the job from docker_ref. sif_path is an HPC
        path; the sbatch script checks it exists.
        """
        ref = configuration.sif_artifact
        if not ref:
            return None
        return await anyio.to_thread.run_sync(
            self._resolve_sif_sync, ref, configuration.image
        )

    @classmethod
    def _resolve_sif_sync(cls, ref: str, image: str | None) -> tuple[str, str]:
        import lamindb as ln

        art = (
            ln.Artifact.filter(key=ref).one_or_none()
            or ln.Artifact.filter(uid=ref).one_or_none()
        )
        if art is not None:
            docker_ref = (
                (art.features.get_values() or {}).get("docker_ref")
                or image
                or cls._infer_docker_ref(art.key or ref)
            )
            return str(art.path), docker_ref
        # Missing artifact: derive the path and ref from the key.
        docker_ref = image or cls._infer_docker_ref(ref)
        if not docker_ref:
            raise RuntimeError(
                f"SIF artifact {ref!r} not found and no ghcr ref given or inferable"
            )
        return f"{os.environ['CLINICALOMX_SITE_HPC_ALIAS'].rstrip('/')}/{ref}", docker_ref

    @classmethod
    def _infer_docker_ref(cls, key: str) -> str | None:
        """environments/<owner>/<name>_<tag>.sif -> <registry host>/<owner>/<name>:<tag>.

        The inverse of clinicalomx.environments.sif_key_for_image. None for any other shape.
        """
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "environments" or not parts[2].endswith(".sif"):
            return None
        owner, name = parts[1], parts[2][:-4]
        if not owner or "_" not in name:
            return None
        repo, tag = name.rsplit("_", 1)
        host = os.environ["CLINICALOMX_SITE_IMAGE_REGISTRY"].rstrip("/").rsplit("/", 1)[0]
        return f"{host}/{owner}/{repo}:{tag}"

    def _build_script(
        self,
        flow_run: FlowRun,
        configuration: HpcSlurmJobConfiguration,
        registry_env: dict[str, str] | None = None,
        sif: tuple[str, str | None] | None = None,
    ) -> str:
        """Render the sbatch script: directives, environment, then the flow command."""
        c = configuration
        directives = [
            f"--job-name={c.job_name}",
            f"--account={c.account}",
            f"--partition={c.partition}",
            f"--qos={c.qos}",
            f"--nodes={c.nodes}",
            f"--ntasks-per-node={c.ntasks_per_node}",
            f"--cpus-per-task={c.cpus_per_task}",
            f"--mem={c.mem}",
            f"--time={c.walltime}",
            f"--output={c.output}",
            f"--error={c.error}",
        ]
        if c.gres:
            directives.append(f"--gres={c.gres}")
        if c.arch_constraint:
            directives.append(f"--constraint={c.arch_constraint}")
        if c.array:
            directives.append(f"--array={c.array}")

        lines = ["#!/bin/bash --login", ""]
        lines += [f"#SBATCH {d}" for d in directives]
        lines.append("#SBATCH --export=ALL")
        lines.append("")
        lines.append("set -eo pipefail")
        lines.append(
            'echo "[hpc-worker] flow run starting on $(hostname) at $(date)"'
        )
        lines.append("")

        if c.apptainer_cachedir:
            lines.append(
                f"export APPTAINER_CACHEDIR={shlex.quote(c.apptainer_cachedir)}"
            )

        # Registry credentials, exported twice:
        #   APPTAINER_DOCKER_*               read by apptainer on the host for the pull.
        #   APPTAINERENV_APPTAINER_DOCKER_*  arrives inside the container as APPTAINER_DOCKER_*.
        # apptainer does not forward APPTAINER_-prefixed variables into the container.
        # Never --env: that puts the token in the compute node's process list.
        for key, value in (registry_env or {}).items():
            quoted = shlex.quote(str(value))
            lines.append(f"export {key}={quoted}")
            lines.append(f"export APPTAINERENV_{key}={quoted}")

        # Prefect flow-run environment. apptainer passes unprefixed host variables through.
        for key, value in (c.env or {}).items():
            if value is not None:
                lines.append(f"export {key}={shlex.quote(str(value))}")
        lines += _site_exports()
        # The job reaches the Prefect API over the external URL, not the worker's localhost.
        external_api = os.environ.get("PREFECT_EXTERNAL_API_URL")
        if external_api:
            lines.append(f"export PREFECT_API_URL={shlex.quote(external_api)}")
        mlflow_uri = _external_mlflow_uri()
        if mlflow_uri:
            lines.append(f"export MLFLOW_TRACKING_URI={shlex.quote(mlflow_uri)}")
        lines.append("")

        # Run the flow in the container: the prebuilt SIF when one is resolved, built from
        # docker_ref when its path is missing, else docker://image.
        exec_parts = ["apptainer", "exec"]
        gpu_flag = self._gpu_flag(c)
        if gpu_flag:
            exec_parts.append(gpu_flag)
        if c.bind_paths:
            exec_parts += ["--bind", ",".join(c.bind_paths)]

        if sif is not None:
            sif_path, docker_ref = sif
            lines.append(f"SIF={shlex.quote(sif_path)}")
            if docker_ref:
                # apptainer pull does not create the parent directory.
                lines.append('mkdir -p "$(dirname "$SIF")"')
                lines.append(
                    f'[ -f "$SIF" ] || apptainer pull "$SIF" '
                    f'{shlex.quote("docker://" + docker_ref)}'
                )
            exec_parts.append('"$SIF"')
        else:
            exec_parts.append(f"docker://{c.image}")
        exec_parts.append(c.command or "prefect flow-run execute")
        lines.append(" ".join(exec_parts))
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _gpu_flag(configuration: HpcSlurmJobConfiguration) -> str:
        """apptainer GPU passthrough: --rocm for AMD parts, --nv for NVIDIA."""
        if configuration.gpu in {"mi210", "mi300x"}:
            return "--rocm"
        if configuration.gres:
            return "--nv"
        return ""

    async def run(
        self,
        flow_run: FlowRun,
        configuration: HpcSlurmJobConfiguration,
        task_status: anyio.abc.TaskStatus[int] | None = None,
    ) -> HpcSlurmSSHPushWorkerResult:
        logger = self.get_flow_run_logger(flow_run)

        # Lightweight flows run on the login node, with no SLURM job and no container.
        if configuration.run_on_login_node:
            return await self._run_on_login_node(
                flow_run, configuration, task_status, logger
            )

        registry_env = await self._registry_env(configuration)
        sif = await self._resolve_sif(configuration)
        job_name = configuration.job_name or f"flow-{flow_run.id}"
        declared_mem = configuration.mem
        oom_retries = 0
        started = False

        # Submit and monitor. Resubmit only after an OOM kill, while the flow run still wants it.
        while True:
            script = self._build_script(flow_run, configuration, registry_env, sif)
            # The mem this sbatch carried. _grow_mem_after_oom changes configuration.mem later.
            submitted_mem = configuration.mem
            logger.info(
                f"Submitting SLURM job for flow run {flow_run.id} "
                f"(mem={submitted_mem})"
            )
            job_id = await self._submit_with_retry(
                script, job_name, logger, configuration
            )
            pid = f"hpc:{job_id}"
            logger.info(f"Submitted SLURM job {job_id} (infrastructure pid {pid})")

            # Report the first job's id once; anyio refuses a second started().
            if not started and task_status is not None:
                task_status.started(pid)
                started = True

            try:
                state = await self._monitor(job_id, logger, flow_run, job_name)
            except anyio.get_cancelled_exc_class():
                logger.warning(f"run cancelled - scancel {job_id}")
                with anyio.CancelScope(shield=True):
                    try:
                        await self._scancel(job_id)
                    except Exception as exc:
                        logger.warning(f"scancel {job_id} failed during cancel: {exc}")
                raise

            if not self._grow_mem_after_oom(configuration, job_id, state, logger):
                break
            if not await self._run_still_wants_the_job(flow_run, logger):
                break
            oom_retries += 1

        status_code = 0 if state in _SUCCESS_STATES else 1
        if await self._mark_cancelled_if_asked(flow_run, job_id, state, logger):
            # Nonzero makes the base worker propose Crashed, which contradicts Cancelled.
            status_code = 0
        elif status_code == 0:
            logger.info(f"SLURM job {job_id} COMPLETED")
        else:
            logger.error(f"SLURM job {job_id} ended in state {state!r}")
        if oom_retries:
            # An OOM retry means the mem declared in prefect.yaml is too low.
            logger.warning(
                f"flow run {flow_run.id} took {oom_retries} OOM "
                f"{'resubmission' if oom_retries == 1 else 'resubmissions'} to reach "
                f"{state!r}: mem {declared_mem} -> {submitted_mem}. The deployment's "
                f"declared mem is unchanged at {declared_mem} and the next run starts "
                f"there again, so raise it in prefect.yaml if this repeats."
            )
        return HpcSlurmSSHPushWorkerResult(
            identifier=pid, status_code=status_code, slurm_state=state
        )

    def _grow_mem_after_oom(
        self,
        configuration: HpcSlurmJobConfiguration,
        job_id: str,
        state: str,
        logger,
    ) -> bool:
        """Double this run's memory request after an OOM kill. True means resubmit.

        Changes only this run's copy of the configuration. The deployment's `mem` is untouched.
        """
        # sacct qualifies some states ("CANCELLED by 1234"), so compare the first word.
        head = state.split()[0].upper() if state else ""
        if head != _OOM_STATE:
            return False
        ceiling = configuration.oom_retry_max_mem
        if not ceiling:
            return False

        current = configuration.mem
        nxt = _next_mem_request(current, ceiling)
        if nxt is None:
            logger.error(
                f"SLURM job {job_id} was killed {_OOM_STATE} at mem={current}, which the "
                f"oom_retry_max_mem={ceiling} ceiling leaves nothing to double into. NOT "
                f"resubmitting: this job needs less memory rather than more."
            )
            return False

        clamped = ""
        if mem_kib(nxt) < mem_kib(current) * 2:
            clamped = (
                f" Doubling would have asked for "
                f"{format_mem(mem_kib(current) * 2)}, over the ceiling, so the request is "
                f"the ceiling itself and this is the last attempt."
            )
        logger.warning(
            f"SLURM job {job_id} was killed {_OOM_STATE} at mem={current}; resubmitting "
            f"flow run with mem={nxt} (oom_retry_max_mem={ceiling}).{clamped}"
        )
        configuration.mem = nxt
        return True

    async def _run_still_wants_the_job(self, flow_run: FlowRun, logger) -> bool:
        """Whether the flow run still wants a resubmission after an OOM, read from the server.

        A cancelled run or a failed read returns False.
        """
        try:
            current = await self.client.read_flow_run(flow_run.id)
        except Exception as exc:
            logger.error(
                f"could not read the state of flow run {flow_run.id} before an OOM "
                f"resubmission ({exc}); NOT resubmitting, because a job submitted against a "
                f"run that was cancelled in the meantime is one nothing will stop."
            )
            return False

        state = current.state
        if state is not None and (state.is_cancelling() or state.is_final()):
            logger.warning(
                f"flow run {flow_run.id} is {state.name!r}, so the OOM resubmission is "
                f"abandoned rather than sent. Reporting the {_OOM_STATE} kill instead."
            )
            return False
        return True

    async def _run_on_login_node(
        self,
        flow_run: FlowRun,
        configuration: HpcSlurmJobConfiguration,
        task_status: anyio.abc.TaskStatus[int] | None,
        logger,
    ) -> HpcSlurmSSHPushWorkerResult:
        """Run the flow on the HPC login node over SSH, with no sbatch and no container.

        The exit code becomes the result. Needs login_python, a Python with prefect installed.
        """
        c = configuration
        lines = ["set -eo pipefail"]
        for key, value in (c.env or {}).items():
            if value is not None:
                lines.append(f"export {key}={shlex.quote(str(value))}")
        lines += _site_exports()
        external_api = os.environ.get("PREFECT_EXTERNAL_API_URL")
        if external_api:
            lines.append(f"export PREFECT_API_URL={shlex.quote(external_api)}")
        mlflow_uri = _external_mlflow_uri()
        if mlflow_uri:
            lines.append(f"export MLFLOW_TRACKING_URI={shlex.quote(mlflow_uri)}")
        if c.login_python:
            lines.append(
                f"export PATH={shlex.quote(os.path.dirname(c.login_python))}:$PATH"
            )
        lines.append(c.command or "prefect flow-run execute")
        script = "\n".join(lines) + "\n"

        pid = f"login:{flow_run.id}"
        if task_status is not None:
            task_status.started(pid)
        logger.info(f"Running flow run {flow_run.id} on the HPC login node (no SLURM)")
        try:
            await self._ssh("bash -s", stdin=script, timeout=self.login_timeout)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:
            logger.error(f"login-node flow run {flow_run.id} failed: {exc}")
            return HpcSlurmSSHPushWorkerResult(
                identifier=pid, status_code=1, slurm_state="LOGIN_FAILED"
            )
        logger.info(f"login-node flow run {flow_run.id} completed")
        return HpcSlurmSSHPushWorkerResult(
            identifier=pid, status_code=0, slurm_state="LOGIN_COMPLETED"
        )

    async def _monitor(
        self, job_id: str, logger, flow_run: FlowRun, job_name: str
    ) -> str:
        """Poll until the job leaves the queue, then return its sacct state.

        Tolerates max_poll_failures SSH failures in a row. Each poll also checks whether the
        flow run is CANCELLING and scancels the job if so; nothing else cancels a submitted run.
        """
        startup_deadline = time.monotonic() + self.startup_timeout
        job_seen = False
        failures = 0
        scancelled = False

        async def _honour_cancel() -> None:
            nonlocal scancelled
            if scancelled or not await self._cancel_requested(flow_run, logger):
                return
            scancelled = await self._scancel_flow_run_jobs(job_id, job_name, logger)

        while True:
            try:
                state = await self._squeue_state(job_id)
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning(
                    f"squeue poll failed for {job_id} "
                    f"({failures}/{self.max_poll_failures}): {exc}"
                )
                if failures >= self.max_poll_failures:
                    raise
                await anyio.sleep(self.poll_interval_pending)
                continue

            if state is not None:
                job_seen = True
                logger.info(f"SLURM job {job_id} state={state}")
                await _honour_cancel()
                await anyio.sleep(self.poll_interval_running)
            elif job_seen:
                break
            else:
                # Not seen yet, or finished between polls. sacct decides.
                sacct_state = await self._sacct_state(job_id)
                if sacct_state:
                    logger.info(
                        f"SLURM job {job_id} not in squeue; sacct={sacct_state}"
                    )
                    if not sacct_state.endswith("ING"):
                        break
                    job_seen = True
                    await _honour_cancel()
                    await anyio.sleep(self.poll_interval_running)
                elif time.monotonic() > startup_deadline:
                    raise RuntimeError(
                        f"SLURM job {job_id} did not appear in squeue or sacct "
                        f"within {self.startup_timeout}s"
                    )
                else:
                    logger.info(f"SLURM job {job_id} not yet in squeue, waiting...")
                    await anyio.sleep(self.poll_interval_pending)

        # sacct can lag briefly behind the job ending.
        for _ in range(self.max_poll_failures):
            state = await self._sacct_state(job_id)
            if state and not state.endswith("ING"):
                return state
            await anyio.sleep(self.poll_interval_pending)
        return state or "UNKNOWN"

    async def _cancel_requested(self, flow_run: FlowRun, logger) -> bool:
        """Whether the flow run is CANCELLING on the server. An unreadable state is not."""
        try:
            current = await self.client.read_flow_run(flow_run.id)
        except Exception as exc:
            logger.warning(
                f"could not read the state of flow run {flow_run.id} to check for a "
                f"cancel ({exc}); asking again next poll"
            )
            return False
        return current.state is not None and current.state.is_cancelling()

    async def _scancel_flow_run_jobs(self, job_id: str, job_name: str | None, logger) -> bool:
        """scancel this flow run's job by id and by name. True when every scancel was sent.

        After an OOM resubmission the stored id is the dead first job; the name finds the live one.
        """
        sent = True
        logger.info(f"scancel {job_id} (flow run job {job_name or job_id})")
        try:
            await self._scancel(job_id)
        except Exception as exc:
            logger.warning(f"scancel {job_id} failed: {exc}")
            sent = False
        if not job_name:
            return sent
        try:
            live = await self._job_id_by_name(job_name)
        except Exception as exc:
            logger.warning(f"could not look up {job_name!r} after scancel: {exc}")
            return False
        if live and live != job_id:
            logger.warning(
                f"{job_name!r} is still queued as SLURM job {live}, not {job_id} - "
                f"scancel {live} as well (an OOM retry resubmits under a new job id)"
            )
            try:
                await self._scancel(live)
            except Exception as exc:
                logger.warning(f"scancel {live} failed: {exc}")
                sent = False
        return sent

    async def _mark_cancelled_if_asked(
        self, flow_run: FlowRun, job_id: str, state: str, logger
    ) -> bool:
        """Mark a CANCELLING flow run Cancelled. Call only after its job has left the queue."""
        if not await self._cancel_requested(flow_run, logger):
            return False
        try:
            await self.client.set_flow_run_state(
                flow_run.id,
                Cancelled(message=f"SLURM job {job_id} stopped: {state}"),
                force=True,
            )
        except Exception as exc:
            logger.error(
                f"SLURM job {job_id} ended {state!r} but flow run {flow_run.id} could not "
                f"be marked Cancelled ({exc}); it still reads CANCELLING"
            )
            return False
        logger.info(f"SLURM job {job_id} ended {state!r}; flow run marked Cancelled")
        return True

    async def kill_infrastructure(
        self,
        infrastructure_pid: str,
        configuration: HpcSlurmJobConfiguration,
        grace_seconds: int = 30,
    ) -> None:
        """scancel the job, then verify it has left the queue within grace_seconds.

        Prefect calls this only for an unsubmitted run; _monitor cancels submitted ones.
        """
        logger = get_logger("prefect.workers.hpc")
        if not infrastructure_pid.startswith("hpc:"):
            logger.info(f"{infrastructure_pid} is not a SLURM job; nothing to scancel")
            return
        job_id = infrastructure_pid.split(":")[-1]
        job_name = configuration.job_name
        await self._scancel_flow_run_jobs(job_id, job_name, logger)

        async def _gone() -> bool:
            """Nothing for this flow run is left in the queue. Checked by name, else by id."""
            if job_name:
                return await self._job_id_by_name(job_name) is None
            return await self._squeue_state(job_id) is None

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            try:
                if await _gone():
                    logger.info(f"SLURM job {job_id} left the queue after scancel")
                    return
            except Exception as exc:
                logger.warning(f"post-scancel poll failed for {job_id}: {exc}")
            await anyio.sleep(3)
        logger.warning(
            f"SLURM job {job_id} still present {grace_seconds}s after scancel"
        )
