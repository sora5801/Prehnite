"""MCP-server-level tests with a stubbed sandbox.

These call the FastMCP server's `call_tool` interface (async) so the
behaviour we're checking is the same path a real MCP client would take —
without spinning up a stdio transport or hitting Docker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prehnite.mcp_server import _Session, build_server
from prehnite.schemas import Task
from prehnite.trajectory import TrajectoryWriter, read_trajectory


class _FakeSandbox:
    """Stand-in for Sandbox — `note` only touches container_id and stop()."""

    def __init__(self) -> None:
        self.container_id = "fake-container"

    def stop(self) -> None:
        return None


def _seed_session(
    sessions: dict[str, _Session], tmp_path: Path, agent_command_count: int = 0
) -> tuple[str, _Session, TrajectoryWriter]:
    out_path = tmp_path / "traj.jsonl"
    writer = TrajectoryWriter(out_path)
    writer.open()
    sess = _Session(
        task=Task(id="t", description="x"),
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        writer=writer,
        trajectory_path=out_path,
        agent_command_count=agent_command_count,
    )
    sid = "test-session"
    sessions[sid] = sess
    return sid, sess, writer


async def test_note_writes_agent_thought_and_does_not_bump_command_count(
    tmp_path: Path,
) -> None:
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)

    sid, sess, writer = _seed_session(sessions, tmp_path, agent_command_count=3)

    await server.call_tool(
        "note",
        {"session_id": sid, "thought": "the count is off by one"},
    )
    writer.close()

    events = read_trajectory(sess.trajectory_path)
    thoughts = [e for e in events if e.type == "agent_thought"]
    assert len(thoughts) == 1
    assert thoughts[0].data == {"thought": "the count is off by one"}

    # Counter must be untouched — note is not an action.
    assert sess.agent_command_count == 3


async def test_note_unknown_session_raises() -> None:
    server = build_server()
    with pytest.raises(Exception):  # FastMCP wraps the KeyError
        await server.call_tool(
            "note", {"session_id": "nope", "thought": "hi"}
        )
