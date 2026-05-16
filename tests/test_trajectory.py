from __future__ import annotations

import json
from pathlib import Path

import pytest

from prehnite.trajectory import TrajectoryWriter, read_trajectory


def test_write_appends_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "out" / "traj.jsonl"
    with TrajectoryWriter(p) as w:
        w.write("run_started", {"task_id": "hello"})
        w.write("agent_command", {"cmd": "ls", "exit_code": 0})
        w.write("run_finished", {"result": "passed", "reason": "ok"})

    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    parsed = [json.loads(line) for line in lines]
    assert [e["seq"] for e in parsed] == [0, 1, 2]
    assert [e["type"] for e in parsed] == [
        "run_started",
        "agent_command",
        "run_finished",
    ]
    assert parsed[1]["data"] == {"cmd": "ls", "exit_code": 0}


def test_read_trajectory_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "traj.jsonl"
    with TrajectoryWriter(p) as w:
        w.write("run_started", {"task_id": "hello"})
        w.write("run_finished", {"result": "passed", "reason": "ok"})

    events = read_trajectory(p)
    assert [e.type for e in events] == ["run_started", "run_finished"]
    assert events[0].data["task_id"] == "hello"


def test_writer_rejects_writes_after_close(tmp_path: Path) -> None:
    p = tmp_path / "traj.jsonl"
    w = TrajectoryWriter(p)
    w.open()
    w.write("run_started", {})
    w.close()
    with pytest.raises(RuntimeError):
        w.write("run_finished", {})


def test_writer_creates_parent_directory(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nest" / "traj.jsonl"
    with TrajectoryWriter(p) as w:
        w.write("run_started", {})
    assert p.is_file()


def test_reopen_existing_trajectory_continues_seq(tmp_path: Path) -> None:
    """A second writer over an existing file must not restart seq at 0 —
    new events continue from where the prior writer left off. This is the
    invariant the MCP server's session resume relies on."""
    p = tmp_path / "traj.jsonl"
    with TrajectoryWriter(p) as w1:
        w1.write("run_started", {})
        w1.write("agent_command", {"cmd": "echo a"})
        w1.write("agent_command", {"cmd": "echo b"})

    # New writer over the same file (simulates MCP server restart).
    with TrajectoryWriter(p) as w2:
        ev = w2.write("agent_command", {"cmd": "echo c"})
        assert ev.seq == 3  # 0,1,2 from prior + this is 3

    events = read_trajectory(p)
    assert [e.seq for e in events] == [0, 1, 2, 3]
    assert events[-1].data["cmd"] == "echo c"


def test_reopen_empty_file_starts_seq_at_zero(tmp_path: Path) -> None:
    p = tmp_path / "traj.jsonl"
    p.touch()  # empty file exists
    with TrajectoryWriter(p) as w:
        ev = w.write("run_started", {})
    assert ev.seq == 0


def test_each_line_is_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "traj.jsonl"
    with TrajectoryWriter(p) as w:
        for i in range(5):
            w.write("agent_command", {"cmd": f"echo {i}", "exit_code": 0})
    for line in p.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_writer_caps_command_event_streams_when_overflow_dir_set(
    tmp_path: Path,
) -> None:
    """A command event whose stdout overflows the cap gets head-truncated
    and the full original is spilled to <overflow_dir>/<sha256>."""
    p = tmp_path / "traj.jsonl"
    odir = tmp_path / "overflow"
    big = "x" * 20_000
    with TrajectoryWriter(p, overflow_dir=odir, max_stream_bytes=1024) as w:
        ev = w.write(
            "agent_command",
            {
                "cmd": "noisy",
                "exit_code": 0,
                "stdout": big,
                "stderr": "",
                "duration_ms": 1,
            },
        )
    # The event's data was truncated in-place — that's what gets persisted
    # AND what the MCP exec callsite returns to the agent.
    assert len(str(ev.data["stdout"]).encode("utf-8")) <= 1024
    assert ev.data["stdout_truncated"] is True
    assert ev.data["stdout_original_bytes"] == 20_000

    sha = ev.data["stdout_overflow_sha256"]
    assert (odir / str(sha)).read_bytes() == big.encode("utf-8")

    # The JSONL on disk also has the truncated form (not the original).
    parsed = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert len(parsed["data"]["stdout"].encode("utf-8")) <= 1024


def test_writer_leaves_non_command_events_untouched(tmp_path: Path) -> None:
    """A `note`/`agent_thought` event with huge text is NOT capped — the
    cap only applies to command stdout/stderr."""
    p = tmp_path / "traj.jsonl"
    odir = tmp_path / "overflow"
    big = "x" * 20_000
    with TrajectoryWriter(p, overflow_dir=odir, max_stream_bytes=1024) as w:
        ev = w.write("agent_thought", {"thought": big})
    assert ev.data["thought"] == big
    # No overflow file was written for the thought.
    assert not odir.exists() or list(odir.iterdir()) == []


def test_writer_with_no_overflow_dir_preserves_full_output(tmp_path: Path) -> None:
    """Default construction (no overflow_dir) keeps all bytes — used by
    tests and callers that explicitly want raw payloads."""
    p = tmp_path / "traj.jsonl"
    big = "x" * 20_000
    with TrajectoryWriter(p) as w:
        ev = w.write(
            "agent_command",
            {
                "cmd": "noisy",
                "exit_code": 0,
                "stdout": big,
                "stderr": "",
                "duration_ms": 1,
            },
        )
    assert ev.data["stdout"] == big
    assert "stdout_truncated" not in ev.data
