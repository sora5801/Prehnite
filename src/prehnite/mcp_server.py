"""Stdio MCP server that exposes Prehnite tasks to an agent.

The agent drives a task interactively:

    list_tasks(tag?, difficulty?)  -> [{id, description, tags, difficulty}, ...]
    describe_task(task_id)         -> full Task spec as a dict
    start_task(task_id)            -> {session_id, container_id, trajectory_path}
    exec(session_id, cmd)          -> CommandResult
    note(session_id, thought)      -> None                 (records reasoning)
    finish_task(session_id)        -> RunResult            (runs verify, tears down)
    abort_task(session_id)         -> None                 (tears down without verify)

Session state lives in this process. Sessions are keyed by UUID so the agent
can run multiple tasks in parallel if it wants to (it usually won't).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from prehnite.runner import trajectory_path
from prehnite.sandbox import Sandbox, SandboxError
from prehnite.schemas import RunResult, RunStatus, Task
from prehnite.tasks.loader import discover_tasks
from prehnite.trajectory import TrajectoryWriter


@dataclass
class _Session:
    task: Task
    sandbox: Sandbox
    writer: TrajectoryWriter
    trajectory_path: Path
    agent_command_count: int = 0


def _root() -> Path:
    return Path(os.environ.get("PREHNITE_ROOT", Path.cwd())).resolve()


def _tasks_dir() -> Path:
    env = os.environ.get("PREHNITE_TASKS_DIR")
    return Path(env).resolve() if env else _root() / "tasks"


def build_server(
    sessions: dict[str, _Session] | None = None,
) -> FastMCP:
    """Construct a FastMCP server. `sessions` is exposed so tests can inject
    fake sessions without going through start_task (which needs Docker)."""
    server = FastMCP("prehnite")
    if sessions is None:
        sessions = {}

    @server.tool()
    def list_tasks(
        tag: str | None = None,
        difficulty: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks under the configured tasks directory.

        Optional filters: pass `tag` to keep only tasks whose `tags` list
        contains it; pass `difficulty` to keep only tasks whose `difficulty`
        equals it. Both are AND-combined.
        """
        out: list[dict[str, Any]] = []
        for t in discover_tasks(_tasks_dir()):
            if tag is not None and tag not in t.tags:
                continue
            if difficulty is not None and t.difficulty != difficulty:
                continue
            out.append(
                {
                    "id": t.id,
                    "description": t.description,
                    "tags": list(t.tags),
                    "difficulty": t.difficulty,
                }
            )
        return out

    @server.tool()
    def describe_task(task_id: str) -> dict[str, Any]:
        """Return the full task spec for `task_id` (everything in the YAML
        file). Useful when an agent wants the verify steps, setup commands,
        or other context that `list_tasks` doesn't include."""
        return _find_task(task_id).model_dump(mode="json")

    @server.tool()
    def start_task(task_id: str) -> dict[str, Any]:
        """Start a fresh sandbox for `task_id` and run its setup commands.

        Returns the session id you'll pass to `exec` / `finish_task`, the
        container id, and the path the trajectory is being written to.
        """
        task = _find_task(task_id)
        out_path = trajectory_path(task, _root())
        writer = TrajectoryWriter(out_path)
        writer.open()

        def _record_egress(data: dict[str, object]) -> None:
            writer.write("egress_attempt", data)

        sandbox = Sandbox(task, egress_callback=_record_egress)
        try:
            sandbox.start()
        except SandboxError as e:
            writer.write(
                "run_finished",
                {"result": RunStatus.ERROR.value, "reason": f"sandbox start: {e}"},
            )
            writer.close()
            raise

        writer.write(
            "run_started",
            {
                "task_id": task.id,
                "image": task.image,
                "container_id": sandbox.container_id,
                "network": task.network.model_dump(mode="json"),
            },
        )

        # Run setup before handing the session to the agent.
        for cmd in task.setup:
            result = sandbox.exec(cmd)
            writer.write("setup_command", result.model_dump())
            if result.exit_code != 0:
                reason = f"setup command failed (exit {result.exit_code}): {cmd}"
                writer.write(
                    "run_finished",
                    {"result": RunStatus.ERROR.value, "reason": reason},
                )
                writer.close()
                sandbox.stop()
                raise RuntimeError(reason)

        sid = uuid.uuid4().hex
        sessions[sid] = _Session(
            task=task, sandbox=sandbox, writer=writer, trajectory_path=out_path,
        )
        return {
            "session_id": sid,
            "container_id": sandbox.container_id,
            "trajectory_path": str(out_path),
        }

    @server.tool()
    def exec(session_id: str, cmd: str) -> dict[str, Any]:
        """Run a single shell command inside the session's sandbox."""
        sess = _require(sessions, session_id)
        result = sess.sandbox.exec(cmd)
        sess.writer.write("agent_command", result.model_dump())
        sess.agent_command_count += 1
        return result.model_dump()

    @server.tool()
    def note(session_id: str, thought: str) -> None:
        """Record your reasoning here between commands — what you tried, why
        you tried it, what you expect to happen. The more reasoning you
        record, the more useful the trajectory is."""
        sess = _require(sessions, session_id)
        sess.writer.write("agent_thought", {"thought": thought})

    @server.tool()
    def finish_task(session_id: str) -> dict[str, Any]:
        """Run verify, write the final event, tear down the sandbox."""
        sess = sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(f"unknown session_id: {session_id}")

        try:
            verify_failures: list[str] = []
            for cmd in sess.task.verify:
                result = sess.sandbox.exec(cmd)
                sess.writer.write("verify_command", result.model_dump())
                if result.exit_code != 0:
                    verify_failures.append(cmd)

            status = RunStatus.PASSED if not verify_failures else RunStatus.FAILED
            if status is RunStatus.PASSED:
                reason = "all verify checks passed"
            elif sess.agent_command_count == 0:
                reason = "no agent activity (verify ran on untouched workspace)"
            else:
                reason = f"verify failed: {verify_failures}"
            sess.writer.write(
                "run_finished", {"result": status.value, "reason": reason}
            )
            return RunResult(
                task_id=sess.task.id,
                status=status,
                reason=reason,
                trajectory_path=sess.trajectory_path,
                container_id=sess.sandbox.container_id,
            ).model_dump(mode="json")
        finally:
            sess.sandbox.stop()
            sess.writer.close()

    @server.tool()
    def abort_task(session_id: str) -> dict[str, str]:
        """Tear down a session without running verify. Use when the agent gives up."""
        sess = sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(f"unknown session_id: {session_id}")
        sess.writer.write(
            "run_finished",
            {"result": RunStatus.ERROR.value, "reason": "aborted by agent"},
        )
        sess.sandbox.stop()
        sess.writer.close()
        return {"session_id": session_id, "status": "aborted"}

    return server


def _find_task(task_id: str) -> Task:
    for task in discover_tasks(_tasks_dir()):
        if task.id == task_id:
            return task
    raise KeyError(f"unknown task_id: {task_id}")


def _require(sessions: dict[str, _Session], session_id: str) -> _Session:
    sess = sessions.get(session_id)
    if sess is None:
        raise KeyError(f"unknown session_id: {session_id}")
    return sess


def main() -> None:
    """Console-script entry point: serve over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
