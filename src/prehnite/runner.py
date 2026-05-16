"""Run a single task end-to-end and emit a trajectory.

The runner is the orchestrator from the CLAUDE.md spine:

    agent (via MCP) -> runner -> sandbox -> trajectory -> JSONL on disk

It owns the lifecycle: open trajectory, start sandbox, run setup, hand control
to the agent (or run a fixed command list, in headless mode), run verify, write
the final event, tear everything down.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from prehnite.sandbox import Sandbox, SandboxError
from prehnite.schemas import (
    CommandResult,
    EventType,
    RunResult,
    RunStatus,
    Task,
    utcnow_iso,
)
from prehnite.trajectory import TrajectoryWriter


@dataclass
class RunContext:
    """Surface a running task exposes to its agent.

    The MCP server hands one of these to the agent via the `run_task` tool.
    The agent calls `exec()` to run shell commands inside the sandbox; each
    call is recorded as an `agent_command` event.
    """

    task: Task
    _sandbox: Sandbox
    _writer: TrajectoryWriter
    _command_count: int = 0

    def exec(self, cmd: str) -> CommandResult:
        result = _exec_and_record(self._sandbox, self._writer, "agent_command", cmd)
        self._command_count += 1
        return result


AgentFn = Callable[[RunContext], None]
"""An agent is just a function that calls `ctx.exec(...)` until it's done."""


def trajectories_dir(root: Path) -> Path:
    return root / "trajectories"


def trajectory_path(task: Task, root: Path) -> Path:
    """`<root>/trajectories/<task_id>/<utc-timestamp>.jsonl`."""
    stamp = utcnow_iso().replace(":", "").replace("-", "")
    return trajectories_dir(root) / task.id / f"{stamp}.jsonl"


def run(
    task: Task,
    *,
    root: Path,
    agent: AgentFn | None = None,
    agent_commands: Iterable[str] | None = None,
) -> RunResult:
    """Execute a task. Provide either `agent` (interactive) or `agent_commands`
    (headless, fixed list — used by the CLI for smoke tests).

    Returns a `RunResult` summarising the outcome and pointing at the
    trajectory file.
    """
    if agent is not None and agent_commands is not None:
        raise ValueError("pass agent OR agent_commands, not both")

    out_path = trajectory_path(task, root)
    writer = TrajectoryWriter(out_path)
    writer.open()

    sandbox = Sandbox(task)
    container_id: str | None = None

    try:
        try:
            sandbox.start()
        except SandboxError as e:
            writer.write(
                "run_finished",
                {"result": RunStatus.ERROR.value, "reason": f"sandbox start: {e}"},
            )
            return RunResult(
                task_id=task.id,
                status=RunStatus.ERROR,
                reason=str(e),
                trajectory_path=out_path,
                container_id=None,
            )

        container_id = sandbox.container_id
        writer.write(
            "run_started",
            {
                "task_id": task.id,
                "image": task.image,
                "container_id": container_id,
                "network": task.network,
            },
        )

        # Setup: failures here are an `error`, not a `failed` task — the test
        # harness itself broke before the agent got a chance.
        for cmd in task.setup:
            result = _exec_and_record(sandbox, writer, "setup_command", cmd)
            if result.exit_code != 0:
                reason = f"setup command failed (exit {result.exit_code}): {cmd}"
                writer.write(
                    "run_finished",
                    {"result": RunStatus.ERROR.value, "reason": reason},
                )
                return RunResult(
                    task_id=task.id,
                    status=RunStatus.ERROR,
                    reason=reason,
                    trajectory_path=out_path,
                    container_id=container_id,
                )

        # Agent phase.
        agent_command_count = 0
        if agent is not None:
            ctx = RunContext(task=task, _sandbox=sandbox, _writer=writer)
            agent(ctx)
            agent_command_count = ctx._command_count
        elif agent_commands is not None:
            for cmd in agent_commands:
                _exec_and_record(sandbox, writer, "agent_command", cmd)
                agent_command_count += 1

        # Verify: every command must exit 0 for the task to pass.
        verify_failures: list[str] = []
        for cmd in task.verify:
            result = _exec_and_record(sandbox, writer, "verify_command", cmd)
            if result.exit_code != 0:
                verify_failures.append(cmd)

        status = RunStatus.PASSED if not verify_failures else RunStatus.FAILED
        if status is RunStatus.PASSED:
            reason = "all verify checks passed"
        elif agent_command_count == 0:
            reason = "no agent activity (verify ran on untouched workspace)"
        else:
            reason = f"verify failed: {verify_failures}"
        writer.write("run_finished", {"result": status.value, "reason": reason})
        return RunResult(
            task_id=task.id,
            status=status,
            reason=reason,
            trajectory_path=out_path,
            container_id=container_id,
        )

    except Exception as e:
        # Anything unexpected: record it, propagate the resulting RunResult,
        # then re-raise after the trajectory is sealed in `finally`.
        writer.write(
            "run_finished",
            {"result": RunStatus.ERROR.value, "reason": f"unhandled: {e!r}"},
        )
        raise
    finally:
        sandbox.stop()
        writer.close()


def _exec_and_record(
    sandbox: Sandbox,
    writer: TrajectoryWriter,
    event_type: EventType,
    cmd: str,
) -> CommandResult:
    result = sandbox.exec(cmd)
    writer.write(event_type, result.model_dump())
    return result
