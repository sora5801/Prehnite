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


# --- list_tasks filtering + describe_task --------------------------------


def _write_task(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def fake_tasks_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A tasks dir on disk with three deliberately-varied tasks."""
    tdir = tmp_path / "tasks"
    _write_task(
        tdir / "alpha.yaml",
        "id: alpha\ndescription: a\ntags: [smoke]\ndifficulty: trivial\n",
    )
    _write_task(
        tdir / "beta.yaml",
        "id: beta\ndescription: b\ntags: [bug-fix, python]\ndifficulty: easy\n",
    )
    _write_task(
        tdir / "gamma.yaml",
        "id: gamma\ndescription: g\ntags: [bug-fix, regex]\ndifficulty: medium\n",
    )
    monkeypatch.setenv("PREHNITE_TASKS_DIR", str(tdir))
    return tdir


def _ids(rows: list[dict[str, object]]) -> set[str]:
    return {str(r["id"]) for r in rows}


async def test_list_tasks_no_filter_returns_all(fake_tasks_dir: Path) -> None:
    server = build_server()
    _, raw = await server.call_tool("list_tasks", {})
    assert _ids(raw["result"]) == {"alpha", "beta", "gamma"}


async def test_list_tasks_filters_by_tag(fake_tasks_dir: Path) -> None:
    server = build_server()
    _, raw = await server.call_tool("list_tasks", {"tag": "bug-fix"})
    assert _ids(raw["result"]) == {"beta", "gamma"}


async def test_list_tasks_filters_by_difficulty(fake_tasks_dir: Path) -> None:
    server = build_server()
    _, raw = await server.call_tool("list_tasks", {"difficulty": "medium"})
    assert _ids(raw["result"]) == {"gamma"}


async def test_list_tasks_filters_combine_with_and(
    fake_tasks_dir: Path,
) -> None:
    server = build_server()
    _, raw = await server.call_tool(
        "list_tasks", {"tag": "bug-fix", "difficulty": "easy"}
    )
    assert _ids(raw["result"]) == {"beta"}


async def test_describe_task_returns_full_spec(fake_tasks_dir: Path) -> None:
    server = build_server()
    # FastMCP passes dict returns through unwrapped (vs. wrapping list/scalar
    # returns under "result"), so the spec is the raw response itself.
    _, spec = await server.call_tool(
        "describe_task", {"task_id": "beta"}
    )
    assert spec["id"] == "beta"
    assert spec["tags"] == ["bug-fix", "python"]
    assert spec["difficulty"] == "easy"
    # Defaults that come from the model, not the YAML, must round-trip too.
    assert spec["network"] is False
    assert spec["workdir"] == "/workspace"


async def test_describe_task_unknown_id_raises(fake_tasks_dir: Path) -> None:
    server = build_server()
    with pytest.raises(Exception):
        await server.call_tool("describe_task", {"task_id": "no-such-task"})
