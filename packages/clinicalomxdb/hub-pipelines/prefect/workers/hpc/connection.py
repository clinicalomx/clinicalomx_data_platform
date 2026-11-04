"""Prefect blocks for reaching the HPC from the VM: the SSH route and registry credentials.

packages/clinicalomx_pipelines/qptiff_pipeline/register_blocks.py imports ContainerRegistryAuth
from here; its tests/test_register_blocks.py checks the two still match."""

import subprocess
import time

import anyio
from prefect.blocks.core import Block
from pydantic import Field, SecretStr


class SSHConnection(Block):
    """A saved SSH route to the HPC: an ssh alias, a user and the ControlMaster socket it rides."""

    _block_type_name = "SSH Connection"

    host: str = Field(description="Login host or ssh config alias")
    user: str = Field(description="Username on host")
    port: int = Field(description="SSH port", default=22)
    ssh_key: SecretStr | None = Field(
        default=None,
        description="Private key contents or path. Optional if key/sshagent/ControlMAster socket already configured.",
    )
    control_master_path: str | None = Field(
        default=None,
        description=(
            "ControlPath for the multiplexed session, passed to ssh as -o ControlPath. "
            "Accepts the same tokens as ssh_config (%r remote user, %h resolved hostname, "
            "%p port), which ssh expands itself, so this should be the SAME template as the "
            "ssh_config entry rather than a resolved path. Leave None to let ssh take the "
            "ControlPath from ssh_config."
        ),
    )

    @classmethod
    async def connect(cls, name: str, *, verify: bool = False) -> "SSHConnection":
        """Load the saved SSH connection block by name.

        The block is saved by deploy.py. verify=True checks the ControlMaster socket is live first.
        """
        conn = await cls.aload(name)
        if verify:
            await anyio.to_thread.run_sync(conn.assert_connected)
        return conn

    def assert_connected(self) -> None:
        """Run `true` over the ControlMaster socket. Raises RuntimeError if DUO has lapsed."""
        self.run("true", timeout=15)

    def run(
        self,
        command: str,
        timeout: int = 30,
        stdin: str | None = None,
        attempts: int = 1,
        retry_delay: float = 5.0,
    ) -> subprocess.CompletedProcess:
        """Run a command on the remote host over the ControlMaster socket.

        stdin, when given, is piped to the command, as for `sbatch`. Raises RuntimeError on a
        non-zero exit. attempts > 1 retries with linear backoff and re-raises the last error.

        Retries recover only a transient socket failure; a lapsed DUO session needs a human to run
        `ssh hpc true`. Retry only idempotent commands.
        """
        host = self.host
        # ControlPath comes from the block when set; ssh expands its %r/%h/%p tokens.
        # ControlMaster is not set: a human opens the master with DUO, so a missing socket fails.
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.control_master_path:
            argv += ["-o", f"ControlPath={self.control_master_path}"]
        argv += [host, command]

        last: Exception | None = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                result = subprocess.run(
                    argv,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"remote command failed on {host}: {command!r}\n"
                        f"Re-auth if the socket is dead: ssh {host} true\n"
                        f"stderr: {result.stderr}\nstdout: {result.stdout}"
                    )
                return result
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last = exc
                if attempt >= max(1, int(attempts)):
                    break
                print(
                    f"[hpc-worker] ssh {command!r} failed on {host} "
                    f"(attempt {attempt}/{attempts}), retrying in "
                    f"{retry_delay * attempt:.0f}s: {exc}",
                    flush=True,
                )
                time.sleep(retry_delay * attempt)
        raise last


class ContainerRegistryAuth(Block):
    """Credentials for pulling a container image from a registry such as ghcr.io.

    Stored server-side as an encrypted block and named by a deployment's registry_auth_block
    job variable, so the token never lands in git. The pipeline repository that owns the image
    creates it; this worker only loads it.
    """

    _block_type_name = "Container Registry Auth"

    username: str = Field(description="Registry username, e.g. a GitHub username")
    password: SecretStr = Field(
        description="Registry password or token, e.g. a GitHub PAT with read:packages"
    )

    def apptainer_env(self) -> dict[str, str]:
        """Env vars apptainer reads on the host to authenticate a docker:// pull.

        The worker also exports each as ``APPTAINERENV_<KEY>`` for the flow inside the container.
        """
        return {
            "APPTAINER_DOCKER_USERNAME": self.username,
            "APPTAINER_DOCKER_PASSWORD": self.password.get_secret_value(),
        }
