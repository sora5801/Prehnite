"""Runner-level behaviour tests with a stubbed-out Sandbox (no Docker required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prehnite import runner
from prehnite.schemas import CommandResult, RunStatus, Task
from prehnite.trajectory import read_trajectory


class _FakeSandbox:
    """Minimal Sandbox stand-in: returns exit 1 for the literal command `false`,
    exit 0 for everything else. Mirrors the Sandbox surface the runner uses
    (start / exec / stop / container_id)."""

    def __init__(self, task: Task) -> None:
        self.task = task
        self.container_id = "fake-container"

    def start(self) -> None:
        return None

    def exec(self, cmd: str) -> CommandResult:
        exit_code = 1 if cmd == "false" else 0
        return CommandResult(
            cmd=cmd, exit_code=exit_code, stdout="", stderr="", duration_ms=1
        )

    def stop(self) -> None:
        return None


@pytest.fixture
def stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "Sandbox", _FakeSandbox)


def _failing_task() -> Task:
    return Task(id="t", description="x", setup=[], verify=["false"])


def test_failed_verify_with_no_agent_activity_distinguished(
    tmp_path: Path, stub_sandbox: None
) -> None:
    """Empty agent_commands + failing verify must read as 'gave up', not 'tried and missed'."""
    result = runner.run(_failing_task(), root=tmp_path, agent_commands=[])

    assert result.status is RunStatus.FAILED
    assert "no agent activity" in result.reason
    assert "verify failed" not in result.reason

    finished = read_trajectory(result.trajectory_path)[-1]
    assert finished.type == "run_finished"
    assert finished.data["result"] == "failed"
    assert "no agent activity" in finished.data["reason"]  # type: ignore[operator]


def test_failed_verify_with_agent_activity_keeps_verify_failed_reason(
    tmp_path: Path, stub_sandbox: None
) -> None:
    """At least one agent command + failing verify still says 'verify failed: [...]'."""
    result = runner.run(
        _failing_task(), root=tmp_path, agent_commands=["echo something"]
    )

    assert result.status is RunStatus.FAILED
    assert "verify failed" in result.reason
    assert "no agent activity" not in result.reason
