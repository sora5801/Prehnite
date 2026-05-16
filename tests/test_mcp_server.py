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


# --- read_trajectory (in-session reflection) ----------------------------


async def test_read_trajectory_returns_all_events_so_far(
    tmp_path: Path,
) -> None:
    """An agent calling read_trajectory(session_id) sees every event
    recorded in this session: setup, agent_command, agent_thought, all
    of it. This is the read counterpart to note's write."""
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)

    sid, sess, writer = _seed_session(sessions, tmp_path)
    writer.write("setup_command", {
        "cmd": "rm -rf /workspace/*", "exit_code": 0,
        "stdout": "", "stderr": "", "duration_ms": 12,
    })
    writer.write("agent_command", {
        "cmd": "cat data.txt", "exit_code": 0,
        "stdout": "hello\n", "stderr": "", "duration_ms": 8,
    })
    writer.write("agent_thought", {"thought": "let me try replacing the file"})
    writer.write("agent_command", {
        "cmd": "echo new > data.txt", "exit_code": 0,
        "stdout": "", "stderr": "", "duration_ms": 5,
    })
    writer.close()

    _, raw = await server.call_tool("read_trajectory", {"session_id": sid})
    events = raw["result"]

    assert [e["type"] for e in events] == [
        "setup_command", "agent_command", "agent_thought", "agent_command",
    ]
    # The agent's earlier stdout — exactly the thing it'd want to recall.
    assert events[1]["data"]["stdout"] == "hello\n"
    # The agent's own note — the "memory" piece.
    assert events[2]["data"]["thought"] == "let me try replacing the file"


async def test_read_trajectory_since_seq_filters_old_events(
    tmp_path: Path,
) -> None:
    """An agent that already saw events up to seq=N can pass
    since_seq=N+1 to fetch only what's new."""
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)

    sid, sess, writer = _seed_session(sessions, tmp_path)
    for i in range(5):
        writer.write("agent_command", {
            "cmd": f"echo {i}", "exit_code": 0,
            "stdout": f"{i}\n", "stderr": "", "duration_ms": 1,
        })
    writer.close()

    _, raw = await server.call_tool(
        "read_trajectory", {"session_id": sid, "since_seq": 3}
    )
    events = raw["result"]

    # Only seq 3 and 4 should come back.
    assert [e["seq"] for e in events] == [3, 4]


async def test_read_trajectory_does_not_count_as_agent_activity(
    tmp_path: Path,
) -> None:
    """Reading is not acting — agent_command_count must stay where it was.
    A session with only read_trajectory calls would still hit the
    'no agent activity' verdict on verify failure (the carve-out from
    devlog 0002 is keyed on commands, not reads)."""
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)

    sid, sess, writer = _seed_session(sessions, tmp_path, agent_command_count=4)
    writer.write("run_started", {"task_id": "x", "image": "i", "container_id": "c"})
    writer.close()

    await server.call_tool("read_trajectory", {"session_id": sid})

    # Counter unchanged — read_trajectory didn't bump it.
    assert sess.agent_command_count == 4


async def test_read_trajectory_unknown_session_raises() -> None:
    server = build_server()
    with pytest.raises(Exception):
        await server.call_tool("read_trajectory", {"session_id": "nope"})


async def test_read_trajectory_empty_trajectory_returns_empty(
    tmp_path: Path,
) -> None:
    """A session with no events written yet (besides what _seed_session
    set up) should return whatever it has — even if that's nothing."""
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)

    sid, sess, writer = _seed_session(sessions, tmp_path)
    writer.close()  # close without writing anything

    _, raw = await server.call_tool("read_trajectory", {"session_id": sid})
    assert raw["result"] == []


# --- fork / revert ------------------------------------------------------


class _SnapshottingSandbox:
    """Stub Sandbox supporting snapshot/revert. Each snapshot() bumps a
    counter and returns a synthetic id; revert() flips container_id so
    the test can verify the swap happened."""

    def __init__(self) -> None:
        self.container_id = "live-container-0"
        self.snapshots: list[str] = []
        self.snapshot_labels: list[dict[str, str] | None] = []
        self.reverted_to: str | None = None
        self._next_id = 0
        self._next_container = 1

    def snapshot(self, extra_labels: dict[str, str] | None = None) -> str:
        snap_id = f"snap-{self._next_id}"
        self._next_id += 1
        self.snapshots.append(snap_id)
        self.snapshot_labels.append(extra_labels)
        return snap_id

    def revert(self, snapshot_id: str) -> str:
        if snapshot_id not in self.snapshots:
            raise Exception(f"unknown snapshot id: {snapshot_id}")
        self.reverted_to = snapshot_id
        self.container_id = f"live-container-{self._next_container}"
        self._next_container += 1
        return self.container_id

    def stop(self) -> None:
        return None


def _seed_snapshotting_session(
    sessions: dict[str, _Session], tmp_path: Path
) -> tuple[str, _Session, TrajectoryWriter, _SnapshottingSandbox]:
    out_path = tmp_path / "traj.jsonl"
    writer = TrajectoryWriter(out_path)
    writer.open()
    sandbox = _SnapshottingSandbox()
    sess = _Session(
        task=Task(id="t", description="x"),
        sandbox=sandbox,  # type: ignore[arg-type]
        writer=writer,
        trajectory_path=out_path,
    )
    sid = "fork-session"
    sessions[sid] = sess
    return sid, sess, writer, sandbox


async def test_fork_writes_session_forked_event_and_returns_snap_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fork() snapshots the sandbox + records a session_forked event with
    the snapshot id and the current container id."""
    monkeypatch.setenv("PREHNITE_ROOT", str(tmp_path))
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)
    sid, sess, writer, sandbox = _seed_snapshotting_session(sessions, tmp_path)

    _, raw = await server.call_tool("fork", {"session_id": sid})
    writer.close()

    snap_id = raw["snapshot_id"]
    assert snap_id == "snap-0"
    assert sandbox.snapshots == ["snap-0"]
    # The fork tool tags the snapshot image with the session_id label so
    # `prehnite reap` can identify orphans whose session is gone.
    assert sandbox.snapshot_labels == [{"prehnite.session_id": sid}]

    events = read_trajectory(sess.trajectory_path)
    forked = [e for e in events if e.type == "session_forked"]
    assert len(forked) == 1
    assert forked[0].data["snapshot_id"] == "snap-0"
    assert forked[0].data["container_id"] == "live-container-0"


async def test_revert_swaps_container_and_writes_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """revert() rebuilds the container from the snapshot, writes a
    session_reverted event recording both the previous and new
    container_ids, and refreshes the session descriptor."""
    monkeypatch.setenv("PREHNITE_ROOT", str(tmp_path))
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)
    sid, sess, writer, sandbox = _seed_snapshotting_session(sessions, tmp_path)

    # Fork first to create a snapshot to revert to.
    await server.call_tool("fork", {"session_id": sid})
    # Now revert.
    _, raw = await server.call_tool(
        "revert", {"session_id": sid, "snapshot_id": "snap-0"}
    )
    writer.close()

    new_cid = raw["container_id"]
    assert new_cid == "live-container-1"
    assert sandbox.container_id == "live-container-1"
    assert sandbox.reverted_to == "snap-0"

    events = read_trajectory(sess.trajectory_path)
    reverted = [e for e in events if e.type == "session_reverted"]
    assert len(reverted) == 1
    assert reverted[0].data["snapshot_id"] == "snap-0"
    assert reverted[0].data["previous_container_id"] == "live-container-0"
    assert reverted[0].data["new_container_id"] == "live-container-1"

    # The session descriptor should now point at the NEW container id so
    # an MCP restart attaches to the right container.
    sfile = tmp_path / "sessions" / f"{sid}.json"
    assert sfile.is_file()
    payload = _json.loads(sfile.read_text(encoding="utf-8"))
    assert payload["container_id"] == "live-container-1"


async def test_revert_unknown_snapshot_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PREHNITE_ROOT", str(tmp_path))
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)
    _seed_snapshotting_session(sessions, tmp_path)

    with pytest.raises(Exception):
        await server.call_tool(
            "revert",
            {"session_id": "fork-session", "snapshot_id": "nonexistent"},
        )


async def test_fork_unknown_session_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PREHNITE_ROOT", str(tmp_path))
    server = build_server()
    with pytest.raises(Exception):
        await server.call_tool("fork", {"session_id": "nope"})


async def test_revert_does_not_count_as_agent_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fork and revert are control-flow operations, not commands —
    agent_command_count must stay where it was. A session that only
    forks/reverts (no exec) would still hit the 'no agent activity'
    verdict on verify failure."""
    monkeypatch.setenv("PREHNITE_ROOT", str(tmp_path))
    sessions: dict[str, _Session] = {}
    server = build_server(sessions=sessions)
    sid, sess, writer, _ = _seed_snapshotting_session(sessions, tmp_path)
    sess.agent_command_count = 2  # pretend the agent did 2 things already

    await server.call_tool("fork", {"session_id": sid})
    await server.call_tool(
        "revert", {"session_id": sid, "snapshot_id": "snap-0"}
    )

    assert sess.agent_command_count == 2  # untouched by fork/revert


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
    assert spec["network"] == {"mode": "none", "extra_allow": []}
    assert spec["workdir"] == "/workspace"


async def test_describe_task_unknown_id_raises(fake_tasks_dir: Path) -> None:
    server = build_server()
    with pytest.raises(Exception):
        await server.call_tool("describe_task", {"task_id": "no-such-task"})


# --- session persistence + rehydration ----------------------------------


import json as _json
from typing import Any

from prehnite import mcp_server
from prehnite.mcp_server import (
    _count_agent_commands,
    _delete_session_file,
    _persist_session,
    _rehydrate_sessions,
    _session_file_path,
)
from prehnite.sandbox import SandboxError


def _seed_session_file(tmp_path: Path, sid: str, **overrides: Any) -> Path:
    """Write a session JSON descriptor by hand. Mirrors what _persist_session
    would produce so tests can exercise the rehydrate path directly."""
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "session_id": sid,
        "task": Task(id="demo", description="x").model_dump(mode="json"),
        "container_id": "deadbeef",
        "trajectory_path": str(tmp_path / "trajectories" / "demo" / f"{sid}.jsonl"),
        "started_at_iso": "2026-05-16T00:00:00Z",
        "network_mode": "none",
    }
    payload.update(overrides)
    path = sdir / f"{sid}.json"
    path.write_text(_json.dumps(payload), encoding="utf-8")
    return path


def test_persist_session_writes_descriptor(tmp_path: Path) -> None:
    """_persist_session writes a JSON file at sessions/<sid>.json with the
    fields _rehydrate_sessions needs."""
    out_path = tmp_path / "trajectories" / "demo" / "run.jsonl"
    writer = TrajectoryWriter(out_path)
    writer.open()
    sess = _Session(
        task=Task(id="demo", description="x"),
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        writer=writer,
        trajectory_path=out_path,
    )
    _persist_session(tmp_path, "abc123", sess)

    f = _session_file_path(tmp_path, "abc123")
    assert f.is_file()
    payload = _json.loads(f.read_text(encoding="utf-8"))
    assert payload["session_id"] == "abc123"
    assert payload["container_id"] == "fake-container"
    assert payload["network_mode"] == "none"
    assert payload["task"]["id"] == "demo"


def test_delete_session_file_removes_and_is_idempotent(tmp_path: Path) -> None:
    f = _seed_session_file(tmp_path, "abc")
    assert f.exists()
    _delete_session_file(tmp_path, "abc")
    assert not f.exists()
    # Second call must not raise — finish_task and abort_task both call it,
    # and a manual cleanup or crash could mean the file's already gone.
    _delete_session_file(tmp_path, "abc")


def test_count_agent_commands_recovers_from_trajectory(tmp_path: Path) -> None:
    traj = tmp_path / "demo.jsonl"
    with TrajectoryWriter(traj) as w:
        w.write("run_started", {})
        w.write("agent_command", {"cmd": "echo 1", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})
        w.write("agent_thought", {"thought": "thinking"})
        w.write("agent_command", {"cmd": "echo 2", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})
        w.write("agent_command", {"cmd": "echo 3", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})
    assert _count_agent_commands(traj) == 3


# Fakes for the Sandbox(...).attach() path used by _rehydrate_sessions.


class _AttachOKSandbox:
    """Stub Sandbox whose attach() succeeds without touching Docker."""

    def __init__(self, task: Task, egress_callback: Any = None) -> None:
        self.task = task
        self.container_id: str | None = None

    def attach(self, container_id: str) -> None:
        self.container_id = container_id

    def stop(self) -> None:
        return None


class _AttachFailSandbox:
    """Stub Sandbox whose attach() always raises (container is gone)."""

    def __init__(self, task: Task, egress_callback: Any = None) -> None:
        self.task = task

    def attach(self, container_id: str) -> None:
        raise SandboxError(f"container {container_id} not found")


def test_rehydrate_drops_restricted_mode_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Restricted-mode sessions can't resume — the proxy port is gone."""
    f = _seed_session_file(tmp_path, "abc", network_mode="restricted")
    out = _rehydrate_sessions(tmp_path)
    assert out == {}
    assert not f.exists()  # dropped
    assert "restricted" in capsys.readouterr().err.lower()


def test_rehydrate_drops_session_with_missing_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the container died, the session can't resume; drop it cleanly."""
    monkeypatch.setattr(mcp_server, "Sandbox", _AttachFailSandbox)
    f = _seed_session_file(tmp_path, "abc")
    # Create the trajectory file too so it's not the trajectory's absence
    # that's tripping us up.
    traj = tmp_path / "trajectories" / "demo" / "abc.jsonl"
    traj.parent.mkdir(parents=True, exist_ok=True)
    traj.touch()

    out = _rehydrate_sessions(tmp_path)
    assert out == {}
    assert not f.exists()
    assert "gone" in capsys.readouterr().err.lower()


def test_rehydrate_rebuilds_session_when_container_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full happy path: container attach succeeds, writer reopens, counter
    recovered from the existing trajectory."""
    monkeypatch.setattr(mcp_server, "Sandbox", _AttachOKSandbox)
    # Lay down an existing trajectory with 2 agent_command events so the
    # counter recovery has something real to count.
    traj = tmp_path / "trajectories" / "demo" / "run.jsonl"
    with TrajectoryWriter(traj) as w:
        w.write("run_started", {})
        w.write("agent_command", {"cmd": "echo 1", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})
        w.write("agent_command", {"cmd": "echo 2", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})
    _seed_session_file(
        tmp_path,
        "live",
        trajectory_path=str(traj),
    )

    out = _rehydrate_sessions(tmp_path)
    assert "live" in out
    sess = out["live"]
    assert sess.sandbox.container_id == "deadbeef"  # attach received the id
    assert sess.agent_command_count == 2  # recovered from trajectory
    assert sess.trajectory_path == traj


def test_rehydrate_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    """No sessions/ dir means no resumable sessions — return empty dict,
    don't crash."""
    assert _rehydrate_sessions(tmp_path) == {}


def test_rehydrate_skips_malformed_session_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    bad = sdir / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    out = _rehydrate_sessions(tmp_path)
    assert out == {}
    # Malformed file is deleted so it doesn't keep tripping us.
    assert not bad.exists()
    assert "malformed" in capsys.readouterr().err.lower()
