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
from typing import Any, Self, cast

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from prehnite.egress_proxy import DEFAULT_ALLOWLIST, EgressCallback, EgressProxy
from prehnite.schemas import CommandResult, Task


class SandboxError(Exception):
    """Anything that goes wrong talking to Docker."""


class Sandbox:
    """A live Docker container scoped to a single task run.

    Usage:
        with Sandbox(task) as sb:
            result = sb.exec("ls /workspace")
    """

    def __init__(
        self,
        task: Task,
        egress_callback: EgressCallback | None = None,
    ) -> None:
        self.task = task
        self.egress_callback = egress_callback
        self._client: docker.DockerClient | None = None
        self._container: Container | None = None
        self._proxy: EgressProxy | None = None

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

        # Per-mode container kwargs. `restricted` also spins up a per-session
        # egress proxy on the host; the container reaches it via
        # host.docker.internal, which Docker Desktop provides natively and
        # we map explicitly via extra_hosts for Linux Docker compatibility.
        spec = self.task.network
        extra_kwargs: dict[str, Any] = {}
        if spec.mode == "none":
            extra_kwargs["network_mode"] = "none"
        elif spec.mode == "full":
            extra_kwargs["network_mode"] = "bridge"
        elif spec.mode == "restricted":
            if self.egress_callback is None:
                raise SandboxError(
                    "restricted network mode requires an egress_callback "
                    "(pass one when constructing Sandbox)"
                )
            allowlist = set(DEFAULT_ALLOWLIST) | set(spec.extra_allow)
            self._proxy = EgressProxy(allowlist, self.egress_callback)
            port = self._proxy.start()
            proxy_url = f"http://host.docker.internal:{port}"
            extra_kwargs["network_mode"] = "bridge"
            extra_kwargs["extra_hosts"] = {"host.docker.internal": "host-gateway"}
            extra_kwargs["environment"] = {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                # Don't proxy localhost / loopback (e.g. agent's own test servers).
                "NO_PROXY": "localhost,127.0.0.1,::1",
                "no_proxy": "localhost,127.0.0.1,::1",
            }

        try:
            container = self._client.containers.create(
                image=self.task.image,
                command=["sleep", "infinity"],
                working_dir=self.task.workdir,
                detach=True,
                tty=False,
                # Conservative limits — agents misbehave; we don't want a fork
                # bomb to take down the host. Tune later if needed.
                mem_limit="2g",
                pids_limit=512,
                # Don't auto-remove; we remove explicitly in stop() so the
                # caller can inspect it on failure if they want.
                auto_remove=False,
                # Label so `prehnite reap` can identify our containers
                # without relying on the image name (which a user might
                # share with unrelated work) or the command. Also tags the
                # task id so a human poking around with `docker ps` can
                # see which task each container came from.
                labels={"prehnite": "true", "prehnite.task_id": self.task.id},
                **extra_kwargs,
            )
        except ImageNotFound as e:
            self._teardown_proxy()
            raise SandboxError(
                f"image {self.task.image!r} not found locally — build it first "
                f"(see docker/base.Dockerfile)"
            ) from e
        except APIError as e:
            self._teardown_proxy()
            raise SandboxError(f"docker create failed: {e.explanation or e}") from e

        try:
            container.start()
        except APIError as e:
            container.remove(force=True)
            self._teardown_proxy()
            raise SandboxError(f"docker start failed: {e.explanation or e}") from e

        self._container = container

    def attach(self, container_id: str) -> None:
        """Re-bind this Sandbox to an already-running container by id.

        Used to resume a session after the MCP server process restarted —
        the docker container is detached + `sleep infinity`, so it survives
        the host-side process death. We don't re-spin the egress proxy
        here; `restricted` mode can't resume (the container's HTTP_PROXY
        points at a port the now-defunct old proxy owned) and the caller
        must check `task.network.mode` before deciding to attach at all.
        """
        if self._container is not None:
            raise SandboxError("sandbox already attached / started")
        try:
            self._client = docker.from_env()
        except DockerException as e:
            raise SandboxError(f"could not reach Docker daemon: {e}") from e
        try:
            self._container = self._client.containers.get(container_id)
        except NotFound as e:
            raise SandboxError(
                f"container {container_id} not found — cannot resume session"
            ) from e
        except APIError as e:
            raise SandboxError(f"docker attach failed: {e.explanation or e}") from e

    def exec(
        self, cmd: str, *, timeout_seconds: float | None = None
    ) -> CommandResult:
        """Run `cmd` via `sh -c` inside the container.

        Wraps the command with GNU `timeout` so a runaway command can't
        hang the session forever — when the wall clock exceeds the budget
        the process group is killed and `exit_code` comes back as 124
        (the conventional "timed out" code). `timeout_seconds` defaults to
        `task.exec_timeout_seconds`; pass an explicit value to override
        for a single call (e.g. an internal probe with a known fast bound).

        Combined output is captured as separate stdout/stderr streams. We
        don't stream — agents in v0 issue one command at a time, so
        blocking until completion is fine and keeps the trajectory simple.
        """
        if self._container is None:
            raise SandboxError("sandbox is not started")

        effective = (
            timeout_seconds
            if timeout_seconds is not None
            else self.task.exec_timeout_seconds
        )

        start = time.monotonic()
        try:
            exec_result = self._container.exec_run(
                # `timeout` exits 124 when the budget runs out — propagated
                # through to the CommandResult so callers can distinguish.
                cmd=["timeout", f"{effective:g}", "sh", "-c", cmd],
                workdir=self.task.workdir,
                demux=True,
                tty=False,
            )
        except APIError as e:
            raise SandboxError(f"exec failed: {e.explanation or e}") from e
        duration_ms = int((time.monotonic() - start) * 1000)

        # docker SDK stubs type `output` as `int | bytes | None`, but with
        # demux=True the runtime always returns a (stdout, stderr) byte tuple.
        demuxed = cast("tuple[bytes | None, bytes | None] | None", exec_result.output)
        stdout_b, stderr_b = demuxed or (None, None)
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

        self._teardown_proxy()

        if self._client is not None:
            self._client.close()
            self._client = None

    def _teardown_proxy(self) -> None:
        if self._proxy is not None:
            try:
                self._proxy.stop()
            except Exception:  # pragma: no cover — best-effort shutdown
                pass
            self._proxy = None


def _decode(b: bytes | None) -> str:
    if not b:
        return ""
    return b.decode("utf-8", errors="replace")
