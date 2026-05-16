"""Tests for the `prehnite inspect <trajectory>` CLI subcommand.

Renders trajectories through the formatter and asserts on captured
stdout/stderr — no subprocess invocation, just the in-process `main()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prehnite.cli import main
from prehnite.trajectory import TrajectoryWriter


def _write_sample_trajectory(path: Path) -> None:
    """Build one trajectory that exercises every event type."""
    with TrajectoryWriter(path) as w:
        w.write(
            "run_started",
            {
                "task_id": "demo",
                "image": "prehnite-base:latest",
                "container_id": "deadbeef",
                "network": {"mode": "restricted", "extra_allow": []},
            },
        )
        w.write(
            "setup_command",
            {
                "cmd": "rm -rf /workspace/*",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 12,
            },
        )
        w.write(
            "agent_command",
            {
                "cmd": "echo hello",
                "exit_code": 0,
                "stdout": "hello\n",
                "stderr": "",
                "duration_ms": 8,
            },
        )
        w.write(
            "agent_thought",
            {"thought": "I should also check the data file before fixing."},
        )
        w.write(
            "egress_attempt",
            {
                "host": "pypi.org",
                "port": 443,
                "allowed": True,
                "reason": "matched allowlist",
                "duration_ms": 25,
            },
        )
        w.write(
            "egress_attempt",
            {
                "host": "blocked.example",
                "port": 443,
                "allowed": False,
                "reason": "not in allowlist",
                "duration_ms": 0,
            },
        )
        w.write(
            "session_forked",
            {
                "snapshot_id": "abcdef1234567890" * 2,
                "container_id": "1234567890abcdef" * 4,
            },
        )
        w.write(
            "session_reverted",
            {
                "snapshot_id": "abcdef1234567890" * 2,
                "previous_container_id": "1234567890abcdef" * 4,
                "new_container_id": "fedcba0987654321" * 4,
            },
        )
        w.write(
            "verify_command",
            {
                "cmd": "test 1 = 1",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 3,
            },
        )
        w.write(
            "run_finished",
            {"result": "passed", "reason": "all verify checks passed"},
        )


def test_inspect_renders_every_event_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traj = tmp_path / "demo.jsonl"
    _write_sample_trajectory(traj)

    rc = main(["inspect", str(traj)])
    assert rc == 0
    out = capsys.readouterr().out

    # One line each for the deterministic events.
    assert "run_started" in out
    assert "task=demo" in out
    assert "network=restricted" in out  # NetworkSpec dump rendered as mode
    assert "setup_command" in out
    assert "agent_command" in out
    assert "agent_thought" in out
    assert "I should also check" in out
    assert "ALLOWED" in out and "pypi.org:443" in out
    assert "DENIED" in out and "blocked.example:443" in out
    # Fork/revert events: render with short snap/container ids, not raw dict.
    assert "session_forked" in out
    assert "snap=abcdef123456" in out  # truncated to 12 chars
    assert "session_reverted" in out
    assert "prev=1234567890ab -> new=fedcba098765" in out
    assert "verify_command" in out
    assert "passed: all verify checks passed" in out


def test_inspect_summary_includes_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traj = tmp_path / "demo.jsonl"
    _write_sample_trajectory(traj)
    main(["inspect", str(traj)])
    out = capsys.readouterr().out
    # Per-type counts at the bottom (alphabetical order).
    assert "1 agent_command" in out
    assert "1 agent_thought" in out
    assert "2 egress_attempt" in out


def test_inspect_type_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traj = tmp_path / "demo.jsonl"
    _write_sample_trajectory(traj)
    main(["inspect", str(traj), "--type", "agent_thought", "--no-summary"])
    out = capsys.readouterr().out
    assert "agent_thought" in out
    assert "I should also check" in out
    # The filter should suppress everything else.
    assert "agent_command" not in out
    assert "egress_attempt" not in out


def test_inspect_summary_only_skips_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traj = tmp_path / "demo.jsonl"
    _write_sample_trajectory(traj)
    main(["inspect", str(traj), "--summary-only"])
    out = capsys.readouterr().out
    # Per-event prefixes like "[  0]" are gone; summary remains.
    assert "[  0]" not in out
    assert "passed: all verify checks passed" in out


def test_inspect_full_does_not_truncate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traj = tmp_path / "long.jsonl"
    long_cmd = "echo " + "x" * 200
    with TrajectoryWriter(traj) as w:
        w.write(
            "agent_command",
            {
                "cmd": long_cmd,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 1,
            },
        )

    # Default: command should be truncated with "..." marker.
    main(["inspect", str(traj), "--no-summary"])
    truncated_out = capsys.readouterr().out
    assert "..." in truncated_out
    assert long_cmd not in truncated_out

    # --full: full command must appear verbatim.
    main(["inspect", str(traj), "--no-summary", "--full"])
    full_out = capsys.readouterr().out
    assert long_cmd in full_out


def test_inspect_missing_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.jsonl"
    rc = main(["inspect", str(missing)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_inspect_incomplete_trajectory_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trajectory missing run_finished should label the summary as incomplete."""
    traj = tmp_path / "incomplete.jsonl"
    with TrajectoryWriter(traj) as w:
        w.write("run_started", {"task_id": "x", "image": "i", "container_id": "c"})
        w.write(
            "agent_command",
            {"cmd": "true", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1},
        )
    main(["inspect", str(traj), "--summary-only"])
    out = capsys.readouterr().out
    assert "incomplete" in out
