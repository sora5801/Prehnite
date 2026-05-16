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
