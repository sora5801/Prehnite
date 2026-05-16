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
    assert t.network.mode == "none"
    assert t.timeout_seconds == 120
    assert t.exec_timeout_seconds == 60
    assert t.workdir == "/workspace"
    assert t.setup == []
    assert t.verify == []


def test_task_exec_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Task(id="hello", description="x", exec_timeout_seconds=0)


def test_task_exec_timeout_upper_bound() -> None:
    with pytest.raises(ValidationError):
        Task(id="hello", description="x", exec_timeout_seconds=601)


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


def test_agent_thought_event_round_trips() -> None:
    e = TrajectoryEvent(
        seq=4,
        ts="2026-05-16T00:00:00Z",
        type="agent_thought",
        data={"thought": "the count is off by one"},
    )
    again = TrajectoryEvent.model_validate_json(e.model_dump_json())
    assert again.type == "agent_thought"
    assert again.data == {"thought": "the count is off by one"}


# --- network policy ------------------------------------------------------


def test_network_spec_default_is_none() -> None:
    t = Task(id="t", description="x")
    assert t.network.mode == "none"
    assert t.network.extra_allow == []


def test_network_legacy_true_maps_to_full() -> None:
    t = Task.model_validate({"id": "t", "description": "x", "network": True})
    assert t.network.mode == "full"


def test_network_legacy_false_maps_to_none() -> None:
    t = Task.model_validate({"id": "t", "description": "x", "network": False})
    assert t.network.mode == "none"


def test_network_explicit_dict_round_trips() -> None:
    t = Task.model_validate(
        {
            "id": "t",
            "description": "x",
            "network": {"mode": "restricted", "extra_allow": ["foo.example"]},
        }
    )
    assert t.network.mode == "restricted"
    assert t.network.extra_allow == ["foo.example"]


def test_egress_attempt_event_round_trips() -> None:
    e = TrajectoryEvent(
        seq=7,
        ts="2026-05-16T00:00:00Z",
        type="egress_attempt",
        data={
            "host": "pypi.org",
            "port": 443,
            "allowed": True,
            "reason": "matched allowlist",
            "duration_ms": 12,
        },
    )
    again = TrajectoryEvent.model_validate_json(e.model_dump_json())
    assert again.type == "egress_attempt"
    assert again.data["host"] == "pypi.org"
    assert again.data["allowed"] is True


def test_run_result_status_enum() -> None:
    rr = RunResult(
        task_id="hello",
        status=RunStatus.PASSED,
        reason="ok",
        trajectory_path="/tmp/foo.jsonl",  # type: ignore[arg-type]
        container_id=None,
    )
    assert rr.status is RunStatus.PASSED
