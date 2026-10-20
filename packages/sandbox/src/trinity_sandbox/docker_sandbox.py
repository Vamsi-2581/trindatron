"""
Isolated Docker sandbox for agent shell execution.

Security flags are hardcoded constants — they are invariants of the sandbox
guarantee and must never be made configurable. Resource limits (memory, CPU,
PIDs) are configurable because they affect operational performance, not the
security boundary itself.

Fail-closed design: callers must check is_available() before calling run().
If the Docker daemon or the sandbox image is not present, execution is denied.
There is no fallback to host execution.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

# These flags define the security boundary. Changing any one of them weakens
# the isolation guarantee and requires a deliberate security review.
_SECURITY_FLAGS: list[str] = [
    "--rm",                                   # auto-remove container on exit
    "--init",                                 # tini init: proper zombie reaping + signal propagation
    "--network=none",                         # no outbound or inbound network
    "--read-only",                            # container root FS is read-only
    "--tmpfs", "/tmp:size=64m,mode=1777",     # writable /tmp (needed by many programs)
    "--cap-drop=ALL",                         # drop all Linux capabilities
    "--security-opt", "no-new-privileges",    # prevent setuid/capability escalation
    "--user", "1000:1000",                    # non-root: sandbox user uid/gid 1000
    "--env", "HOME=/tmp",                     # safe HOME — never exposes the host home dir
    "--env", "PATH=/usr/local/bin:/usr/bin:/bin",  # minimal PATH
]

_UNAVAILABLE_MSG = (
    "Shell execution requires the Docker sandbox ({image}). "
    "Ensure Docker Desktop is running and the image is built: "
    "docker build -t {image} packages/core/docker/sandbox/"
)


class DockerSandbox:
    """
    Runs a shell command inside an isolated Docker container.

    Each invocation creates a fresh, ephemeral container. Only the session
    workspace directory is mounted; no other host paths are accessible.
    """

    def __init__(
        self,
        image: str = "trinity-sandbox:latest",
        memory_limit: str = "256m",
        cpu_limit: str = "0.5",
        pids_limit: int = 64,
    ) -> None:
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_limit = str(cpu_limit)
        self._pids_limit = pids_limit

    @property
    def unavailable_message(self) -> str:
        return _UNAVAILABLE_MSG.format(image=self._image)

    async def is_available(self) -> bool:
        """
        Return True if Docker is reachable and the sandbox image exists.

        Checks the image (not just the daemon) so that a missing image is
        caught here rather than at run time with a confusing pull error.
        """
        if not shutil.which("docker"):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", self._image,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0
        except (asyncio.TimeoutError, OSError):
            return False

    async def run(
        self,
        command: str,
        workspace_path: Path,
        timeout: int = 30,
    ) -> tuple[bool, str]:
        """
        Execute *command* inside the sandbox container.

        workspace_path is the only host directory mounted into the container
        (at /workspace, read-write). All other host paths are inaccessible.

        Returns (success, output). On timeout, all container processes are
        killed via `docker kill` before returning.
        """
        container_name = f"trinity-{uuid.uuid4().hex[:12]}"
        workspace_str = str(workspace_path.resolve())

        docker_args = [
            "docker", "run",
            "--name", container_name,
            *_SECURITY_FLAGS,
            f"--memory={self._memory_limit}",
            f"--cpus={self._cpu_limit}",
            f"--pids-limit={self._pids_limit}",
            "--volume", f"{workspace_str}:/workspace:rw",
            "--workdir", "/workspace",
            self._image,
            "/bin/bash", "-c", command,
        ]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._kill_container(container_name)
                if proc:
                    proc.kill()
                    await proc.communicate()
                return False, f"Command timed out after {timeout}s"

            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode == 0:
                return True, output or "(no output)"
            return False, f"Exit code {proc.returncode}:\n{output}"

        except FileNotFoundError:
            return False, "docker executable not found in PATH"
        except Exception as exc:
            return False, f"Sandbox error: {exc}"

    async def _kill_container(self, name: str) -> None:
        """Force-kill a named container, swallowing all errors."""
        try:
            kill = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill.communicate(), timeout=5.0)
        except Exception:
            pass
