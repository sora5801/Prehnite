"""Sandbox integration tests.

These hit a real Docker daemon and the prehnite-base:latest image. They are
skipped automatically if either is missing — CI without Docker still passes
the rest of the suite.
"""

from __future__ import annotations

import pytest

docker = pytest.importorskip("docker")

from docker.errors import DockerException, ImageNotFound  # noqa: E402

from prehnite.sandbox import Sandbox  # noqa: E402
from prehnite.schemas import Task  # noqa: E402

IMAGE = "prehnite-base:latest"


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
        return True
    except (DockerException, ImageNotFound):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason=f"Docker daemon not reachable or {IMAGE} not built",
    ),
]


def _task(**overrides: object) -> Task:
    base = {"id": "sb-test", "description": "sandbox test", "image": IMAGE}
    base.update(overrides)
    return Task.model_validate(base)


def test_exec_captures_stdout() -> None:
    with Sandbox(_task()) as sb:
        result = sb.exec("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_exec_captures_nonzero_exit() -> None:
    with Sandbox(_task()) as sb:
        result = sb.exec("exit 7")
    assert result.exit_code == 7


def test_exec_separates_stdout_and_stderr() -> None:
    with Sandbox(_task()) as sb:
        result = sb.exec("echo out; echo err 1>&2")
    assert "out" in result.stdout
    assert "err" in result.stderr


def test_workdir_is_task_workdir() -> None:
    with Sandbox(_task(workdir="/tmp")) as sb:
        result = sb.exec("pwd")
    assert result.stdout.strip() == "/tmp"


def test_network_disabled_by_default() -> None:
    with Sandbox(_task()) as sb:
        # `getent hosts` returns nonzero when DNS resolution fails. With
        # network_mode=none there is no resolver.
        result = sb.exec("getent hosts example.com")
    assert result.exit_code != 0


def test_exec_times_out_with_exit_124() -> None:
    """A command that exceeds the per-exec timeout must be killed and
    reported as exit 124 (the GNU `timeout` convention), not hang."""
    import time as _time

    start = _time.monotonic()
    with Sandbox(_task(exec_timeout_seconds=1)) as sb:
        result = sb.exec("sleep 5")
    elapsed = _time.monotonic() - start

    assert result.exit_code == 124
    # Container startup dominates; the exec itself should be ~1s, not 5s.
    # Allow generous slack for daemon/Docker Desktop latency on Windows.
    assert elapsed < 30, f"exec didn't get killed in time: {elapsed:.1f}s"
