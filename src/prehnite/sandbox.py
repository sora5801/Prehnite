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
import uuid
from types import TracebackType
from typing import Any, Self, cast

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from prehnite.egress_proxy import DEFAULT_ALLOWLIST, EgressCallback, EgressProxy
from prehnite.schemas import CommandResult, Task

# Docker image repository used for all session snapshots (fork/revert).
# Per-snapshot tag is a UUID; the `prehnite.snapshot=true` label on each
# image lets the reaper find orphans.
SNAPSHOT_REPO = "prehnite-snapshot"


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
        # Snapshot ids created via snapshot() for this sandbox. Deleted on
        # stop() so per-session snapshot images don't outlive the session.
        self._snapshot_ids: list[str] = []

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

        # Restricted-mode sessions spin up a per-session egress proxy on
        # the host; the container reaches it via host.docker.internal
        # (Docker Desktop provides this natively; we add it via
        # extra_hosts for Linux Docker compatibility). The proxy stays
        # alive across reverts so a forked container can use the same
        # HTTP_PROXY env without churning ports.
        spec = self.task.network
        if spec.mode == "restricted":
            if self.egress_callback is None:
                raise SandboxError(
                    "restricted network mode requires an egress_callback "
                    "(pass one when constructing Sandbox)"
                )
            allowlist = set(DEFAULT_ALLOWLIST) | set(spec.extra_allow)
            self._proxy = EgressProxy(allowlist, self.egress_callback)
            self._proxy.start()

        try:
            container = self._client.containers.create(
                **self._make_container_kwargs()
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

    def _make_container_kwargs(self, image: str | None = None) -> dict[str, Any]:
        """Build the docker create() kwargs for this sandbox. Used by both
        start() (default image) and revert() (a snapshot image). For
        restricted mode, assumes the egress proxy is already running."""
        spec = self.task.network
        extra_kwargs: dict[str, Any] = {}
        if spec.mode == "none":
            extra_kwargs["network_mode"] = "none"
        elif spec.mode == "full":
            extra_kwargs["network_mode"] = "bridge"
        elif spec.mode == "restricted":
            if self._proxy is None:
                raise SandboxError(
                    "restricted mode requires the egress proxy to be running"
                )
            proxy_url = f"http://host.docker.internal:{self._proxy.port}"
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

        return {
            "image": image or self.task.image,
            "command": ["sleep", "infinity"],
            "working_dir": self.task.workdir,
            "detach": True,
            "tty": False,
            # Conservative limits — agents misbehave; we don't want a fork
            # bomb to take down the host. Tune later if needed.
            "mem_limit": "2g",
            "pids_limit": 512,
            # Don't auto-remove; we remove explicitly in stop() so the
            # caller can inspect it on failure if they want.
            "auto_remove": False,
            # Label so `prehnite reap` can identify our containers
            # without relying on the image name. Also tags the task id
            # so a human poking around with `docker ps` can see which
            # task each container came from.
            "labels": {
                "prehnite": "true",
                "prehnite.task_id": self.task.id,
            },
            **extra_kwargs,
        }

    def snapshot(self, extra_labels: dict[str, str] | None = None) -> str:
        """Commit the current container to an image and return a snapshot
        id. Use revert(snap_id) to roll the sandbox back to this state.

        `extra_labels` are added to the snapshot image on top of the
        always-present `prehnite.snapshot=true`. The MCP server uses this
        to tag snapshots with their session id so the reaper can find
        orphans (snapshots whose owning session is gone)."""
        if self._container is None or self._client is None:
            raise SandboxError("sandbox is not started")

        snap_id = uuid.uuid4().hex
        # Each LABEL line becomes a Dockerfile instruction at commit time.
        label_changes = ['LABEL prehnite.snapshot="true"']
        for k, v in (extra_labels or {}).items():
            # Defensive: keep label values printable + quote-safe.
            safe = str(v).replace('"', "")
            label_changes.append(f'LABEL {k}="{safe}"')

        try:
            self._container.commit(
                repository=SNAPSHOT_REPO,
                tag=snap_id,
                changes=label_changes,
            )
        except APIError as e:
            raise SandboxError(f"docker commit failed: {e.explanation or e}") from e

        self._snapshot_ids.append(snap_id)
        return snap_id

    def revert(self, snapshot_id: str) -> str:
        """Stop+remove the current container; create a new one from the
        snapshot image; start it. Returns the new container's id. The
        egress proxy (if restricted mode) stays running across the swap."""
        if snapshot_id not in self._snapshot_ids:
            raise SandboxError(f"unknown snapshot id: {snapshot_id}")
        if self._client is None:
            raise SandboxError("sandbox is not started")

        old = self._container
        self._container = None
        if old is not None:
            try:
                old.stop(timeout=2)
            except (APIError, NotFound):
                pass
            try:
                old.remove(force=True)
            except (APIError, NotFound):
                pass

        image = f"{SNAPSHOT_REPO}:{snapshot_id}"
        try:
            container = self._client.containers.create(
                **self._make_container_kwargs(image=image)
            )
        except (APIError, ImageNotFound) as e:
            raise SandboxError(
                f"revert: could not create container from {image}: {e}"
            ) from e
        try:
            container.start()
        except APIError as e:
            try:
                container.remove(force=True)
            except (APIError, NotFound):
                pass
            raise SandboxError(
                f"revert: docker start failed: {e.explanation or e}"
            ) from e

        self._container = container
        return str(container.id or "")

    def delete_snapshots(self) -> None:
        """Remove every snapshot image this sandbox created. Called from
        stop() so per-session snapshots can't outlive the session."""
        if self._client is None or not self._snapshot_ids:
            return
        for snap_id in self._snapshot_ids:
            try:
                self._client.images.remove(
                    f"{SNAPSHOT_REPO}:{snap_id}", force=True
                )
            except (APIError, NotFound):
                pass  # already gone — fine
        self._snapshot_ids.clear()

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

        # Snapshots before the client closes — we need self._client.images
        # to remove the images.
        self.delete_snapshots()
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
