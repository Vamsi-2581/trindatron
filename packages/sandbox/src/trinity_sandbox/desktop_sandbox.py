"""
Session-scoped Desktop sandbox for Computer Use actions.

Architecture: Xvfb virtual display (:99) inside an isolated Docker container.
PyAutoGUI runs ONLY inside the container — never in the host Python process.
The host Windows desktop is unreachable by design (no X11 socket mounted,
no network to reach an external X11 server, --network=none).

Key differences from DockerSandbox (shell):
  - Persistent container per session (docker run -d), not ephemeral
  - Actions executed via docker exec, not docker run
  - Container carries display state between actions (open windows persist)
  - Destroyed explicitly via stop() at session end

Fail-closed: if the image is missing or the container is not running,
no action is executed — there is no fallback to host execution.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

# Security flags that define the isolation boundary.
# These are invariants — changing any one weakens the security guarantee.
_DESKTOP_SECURITY_FLAGS: list[str] = [
    "--init",                                      # tini: correct signal propagation
    "--network=none",                              # no outbound/inbound network
    "--read-only",                                 # container root FS is read-only
    "--tmpfs", "/tmp:size=256m,mode=1777",         # writable /tmp for X11 socket + HOME
    "--cap-drop=ALL",                              # drop all Linux capabilities
    "--security-opt", "no-new-privileges",         # block setuid/capability escalation
    "--user", "1000:1000",                         # non-root: sandbox user uid/gid 1000
    "--env", "HOME=/tmp",                          # safe HOME, never the host home dir
    "--env", "PATH=/usr/local/bin:/usr/bin:/bin",  # minimal PATH
    "--env", "DISPLAY=:99",                        # virtual display ONLY — never :0 or TCP
    "--env", "PYTHONDONTWRITEBYTECODE=1",          # no .pyc on read-only FS
    "--env", "DBUS_SESSION_BUS_ADDRESS=",          # no dbus
]

_UNAVAILABLE_MSG = (
    "Desktop sandbox not started. Ensure Docker Desktop is running, "
    "the image is built (docker build -t {image} packages/core/docker/desktop-sandbox/), "
    "and start() has been called."
)


class DesktopSandbox:
    """
    Persistent container with a virtual X11 display (Xvfb :99).

    Lifecycle:
        sandbox = DesktopSandbox()
        await sandbox.start(session_id, workspace)   # docker run -d
        await sandbox.exec("pyautogui command")      # docker exec
        await sandbox.screenshot("out.png")          # scrot → /workspace/out.png
        await sandbox.stop()                         # docker stop + rm
    """

    def __init__(
        self,
        image: str = "trinity-desktop-sandbox:latest",
        memory_limit: str = "512m",
        cpu_limit: str = "1.0",
        pids_limit: int = 128,
    ) -> None:
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_limit = str(cpu_limit)
        self._pids_limit = pids_limit
        self._container_name: str | None = None

    @property
    def unavailable_message(self) -> str:
        return _UNAVAILABLE_MSG.format(image=self._image)

    async def image_available(self) -> bool:
        """Return True if Docker is reachable and the desktop image exists."""
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

    async def start(self, session_id: str, workspace: Path) -> bool:
        """
        Start a persistent container for this session.

        Returns True if the container started and Xvfb is ready.
        Returns False (fail-closed) if the image is missing or Docker unavailable.
        """
        if not await self.image_available():
            return False

        container_name = f"trinity-desktop-{session_id}"
        workspace_str = str(workspace.resolve())

        # Remove any stale container with the same name from a previous run
        try:
            cleanup = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.communicate(), timeout=10.0)
        except Exception:
            pass

        docker_args = [
            "docker", "run", "-d",
            "--name", container_name,
            *_DESKTOP_SECURITY_FLAGS,
            f"--memory={self._memory_limit}",
            f"--cpus={self._cpu_limit}",
            f"--pids-limit={self._pids_limit}",
            "--volume", f"{workspace_str}:/workspace:rw",
            "--workdir", "/workspace",
            self._image,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0:
                return False
        except (asyncio.TimeoutError, OSError):
            return False

        self._container_name = container_name

        # Verify Xvfb is ready (entrypoint waits, but give it a moment too)
        for _ in range(20):
            ok, _ = await self.exec("xdpyinfo -display :99 > /dev/null 2>&1 && echo READY", timeout=5)
            if ok:
                return True
            await asyncio.sleep(0.5)

        # Xvfb did not start in time — fail-closed
        await self.stop()
        return False

    async def is_running(self) -> bool:
        """Return True if the container is alive and Xvfb is responsive."""
        if not self._container_name:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect",
                "--format", "{{.State.Running}}",
                self._container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return stdout.decode().strip() == "true"
        except (asyncio.TimeoutError, OSError):
            return False

    async def exec(self, command: str, timeout: int = 30) -> tuple[bool, str]:
        """
        Execute *command* inside the running container with DISPLAY=:99.

        On timeout the exec is aborted but the container is left running
        so subsequent actions can continue in the same session.

        Returns (success, output).
        """
        if not self._container_name:
            return False, self.unavailable_message

        exec_args = [
            "docker", "exec",
            self._container_name,
            "/bin/bash", "-c",
            f"export DISPLAY=:99; {command}",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass
                return False, f"Action timed out after {timeout}s"

            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode == 0:
                return True, output or "(no output)"
            return False, f"Exit code {proc.returncode}:\n{output}"

        except FileNotFoundError:
            return False, "docker executable not found in PATH"
        except Exception as exc:
            return False, f"Desktop sandbox error: {exc}"

    async def screenshot(self, filename: str = "screenshot.png") -> tuple[bool, str]:
        """
        Capture the virtual display to /workspace/{filename}.

        Returns (True, filename) on success, (False, error_message) on failure.
        """
        safe_name = filename.replace("'", "").replace(";", "").replace("&", "")
        ok, output = await self.exec(
            f"scrot -z /workspace/{safe_name} && echo SCREENSHOT_OK",
            timeout=10,
        )
        if ok and "SCREENSHOT_OK" in output:
            return True, safe_name
        return False, f"Screenshot failed: {output}"

    async def stop(self) -> None:
        """Stop and remove the container. Safe to call even if not started."""
        if not self._container_name:
            return
        name = self._container_name
        self._container_name = None

        for cmd in [["docker", "stop", name], ["docker", "rm", "-f", name]]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), timeout=15.0)
            except Exception:
                pass
