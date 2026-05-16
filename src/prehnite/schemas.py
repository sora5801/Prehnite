"""Pydantic models for tasks, trajectory events, and run results.

Everything that crosses a module boundary in Prehnite goes through these models.
That keeps validation in one place and lets mypy reason about shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TaskId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
"""Lowercase alphanumeric + `_`/`-`, 1–64 chars. Forms part of file paths."""


def utcnow_iso() -> str:
    """UTC timestamp with second precision, suffixed `Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Task(BaseModel):
    """A single, repeatable coding task an agent runs against."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: TaskId
    description: str = Field(min_length=1)
    image: str = Field(default="prehnite-base:latest", min_length=1)
    network: bool = False
    timeout_seconds: int = Field(default=120, gt=0, le=3600)
    workdir: str = "/workspace"
    setup: list[str] = Field(default_factory=list)
    verify: list[str] = Field(default_factory=list)


class RunStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CommandResult(BaseModel):
    """Outcome of a single shell command executed inside the sandbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)


EventType = Literal[
    "run_started",
    "setup_command",
    "agent_command",
    "agent_thought",
    "verify_command",
    "run_finished",
]


class TrajectoryEvent(BaseModel):
    """One line of a trajectory JSONL file.

    `data` is loosely typed on purpose: each event type has a different shape,
    but we don't want a giant tagged-union here for v0. The writer enforces
    consistency at the call site.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    ts: str
    type: EventType
    data: dict[str, object] = Field(default_factory=dict)


class RunResult(BaseModel):
    """What the runner returns to its caller (CLI or MCP)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    status: RunStatus
    reason: str
    trajectory_path: Path
    container_id: str | None
