"""Docker container lifecycle for Prehnite sandboxes.

CLAUDE.md invariant: every Docker interaction in the codebase must funnel
through this module. Callers get a `Sandbox` context manager; nothing else
talks to the daemon directly.

A sandbox is one ephemeral container per task run, started detached, with
network disabled by default. We exec commands into it via the Docker SDK and
return structured `CommandResult`s.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Self

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from prehnite.schemas import CommandResult, Task


class SandboxError(Exception):
    """Anything that goes wrong talking to Docker."""


class Sandbox:
    """A live Docker container scoped to a single task run.

    Usage:
        with Sandbox(task) as sb:
            result = sb.exec("ls /workspace")
    """

    def __init__(self, task: Task) -> None:
        self.task = task
        self._client: docker.DockerClient | None = None
        self._container: Container | None = None

    @property
    def container_id(self) -> str | None:
        return self._container.id if self._container is not None else None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        if self._container is not None:
            return
        try:
            self._client = docker.from_env()
        except DockerException as e:
            raise SandboxError(f"could not reach Docker daemon: {e}") from e

        # Detached container with `sleep infinity` so we can exec commands into
        # it. Network is opt-in per task; default `none` keeps runs air-gapped.
        try:
            container = self._client.containers.create(
                image=self.task.image,
                command=["sleep", "infinity"],
                working_dir=self.task.workdir,
                network_mode="bridge" if self.task.network else "none",
                detach=True,
                tty=False,
                # Conservative limits — agents misbehave; we don't want a fork
                # bomb to take down the host. Tune later if needed.
                mem_limit="2g",
                pids_limit=512,
                # Don't auto-remove; we remove explicitly in stop() so the
                # caller can inspect it on failure if they want.
                auto_remove=False,
            )
        except ImageNotFound as e:
            raise SandboxError(
                f"image {self.task.image!r} not found locally — build it first "
                f"(see docker/base.Dockerfile)"
            ) from e
        except APIError as e:
            raise SandboxError(f"docker create failed: {e.explanation or e}") from e

        try:
            container.start()
        except APIError as e:
            container.remove(force=True)
            raise SandboxError(f"docker start failed: {e.explanation or e}") from e

        self._container = container

    def exec(self, cmd: str) -> CommandResult:
        """Run `cmd` via `sh -c` inside the container.

        Combined output is captured as separate stdout/stderr streams. We don't
        stream — agents in v0 issue one command at a time, so blocking until
        completion is fine and keeps the trajectory simple.
        """
        if self._container is None:
            raise SandboxError("sandbox is not started")

        start = time.monotonic()
        try:
            exec_result = self._container.exec_run(
                cmd=["sh", "-c", cmd],
                workdir=self.task.workdir,
                demux=True,
                tty=False,
            )
        except APIError as e:
            raise SandboxError(f"exec failed: {e.explanation or e}") from e
        duration_ms = int((time.monotonic() - start) * 1000)

        stdout_b, stderr_b = exec_result.output or (None, None)
        return CommandResult(
            cmd=cmd,
            exit_code=int(exec_result.exit_code or 0),
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_ms=duration_ms,
        )

    def stop(self) -> None:
        container = self._container
        self._container = None
        if container is not None:
            try:
                container.stop(timeout=2)
            except (APIError, NotFound):
                pass
            try:
                container.remove(force=True)
            except (APIError, NotFound):
                pass

        if self._client is not None:
            self._client.close()
            self._client = None


def _decode(b: bytes | None) -> str:
    if not b:
        return ""
    return b.decode("utf-8", errors="replace")
