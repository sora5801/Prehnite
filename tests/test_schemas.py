from __future__ import annotations

import pytest
from pydantic import ValidationError

from prehnite.schemas import (
    CommandResult,
    RunResult,
    RunStatus,
    Task,
    TrajectoryEvent,
)


def test_task_minimal_defaults() -> None:
    t = Task(id="hello", description="say hi")
    assert t.image == "prehnite-base:latest"
    assert t.network is False
    assert t.timeout_seconds == 120
    assert t.workdir == "/workspace"
    assert t.setup == []
    assert t.verify == []


def test_task_id_pattern_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        Task(id="Hello", description="x")


def test_task_id_pattern_rejects_spaces() -> None:
    with pytest.raises(ValidationError):
        Task(id="he llo", description="x")


def test_task_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Task(id="hello", description="x", timeout_seconds=0)


def test_task_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(
            {"id": "hello", "description": "x", "what_is_this": True}
        )


def test_command_result_round_trip() -> None:
    r = CommandResult(cmd="ls", exit_code=0, stdout="a\n", stderr="", duration_ms=5)
    again = CommandResult.model_validate_json(r.model_dump_json())
    assert again == r


def test_trajectory_event_seq_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        TrajectoryEvent(seq=-1, ts="2026-01-01T00:00:00Z", type="run_started")


def test_run_result_status_enum() -> None:
    rr = RunResult(
        task_id="hello",
        status=RunStatus.PASSED,
        reason="ok",
        trajectory_path="/tmp/foo.jsonl",  # type: ignore[arg-type]
        container_id=None,
    )
    assert rr.status is RunStatus.PASSED
