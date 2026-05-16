"""Tests for `prehnite batch <tasks-dir> --agent <cmd>`.

We monkeypatch `subprocess.run` so each test gets to script the "agent"
deterministically — either writing a trajectory, hanging until timeout,
or exiting without producing any trajectory at all. No real subprocess
invocation, no Docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from prehnite import cli
from prehnite.cli import main
from prehnite.trajectory import TrajectoryWriter


def _write_task(tasks_dir: Path, task_id: str) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"{task_id}.yaml").write_text(
        f"id: {task_id}\ndescription: x\n", encoding="utf-8"
    )


def _write_trajectory(
    root: Path, task_id: str, result: str, reason: str = ""
) -> Path:
    out = root / "trajectories" / task_id / f"{task_id}_run.jsonl"
    with TrajectoryWriter(out) as w:
        w.write(
            "run_started",
            {"task_id": task_id, "image": "i", "container_id": "c"},
        )
        w.write("run_finished", {"result": result, "reason": reason})
    return out


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, fn: Callable[[str], Any]
) -> None:
    """Replace subprocess.run with a wrapper that calls `fn(cmd)` and returns
    a SimpleNamespace mimicking a CompletedProcess. `fn` is the test's agent
    impersonator — it has total freedom over what it does (write a trajectory,
    raise TimeoutExpired, exit non-zero)."""

    def _fake_run(cmd: str, **kwargs: Any) -> Any:
        return fn(cmd)

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)


def test_batch_records_passed_outcome_from_trajectory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    def fake_agent(cmd: str) -> Any:
        assert "demo" in cmd, "task id should be substituted into the cmd"
        _write_trajectory(tmp_path, "demo", "passed", "all verify checks passed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "fake-agent --task {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "demo" in out
    assert "passed" in out
    assert "pass-rate:     100%" in out
    assert rc == 0


def test_batch_records_failed_outcome_and_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    def fake_agent(cmd: str) -> Any:
        _write_trajectory(tmp_path, "demo", "failed", "verify failed: [...]")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "fake {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "failed" in out
    # Even one non-pass means the batch exits non-zero — pipelines need to see it.
    assert rc == 1


def test_batch_marks_agent_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "wedged")

    def fake_agent(cmd: str) -> Any:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "wedged {task_id}",
            "--per-task-timeout",
            "1",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "agent_timeout" in out
    assert rc == 1


def test_batch_marks_no_trajectory_when_agent_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "ghost")

    def fake_agent(cmd: str) -> Any:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "noop {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "no_trajectory" in out
    assert rc == 1


def test_batch_ignores_pre_existing_trajectories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trajectory written before the batch starts must NOT be counted as
    this run's outcome. Locks in the mtime-based filtering."""
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    # Pre-existing trajectory from a long-ago run.
    old_traj = _write_trajectory(tmp_path, "demo", "passed", "old")
    import os
    os.utime(old_traj, (1_000_000.0, 1_000_000.0))  # very old mtime

    def fake_agent(cmd: str) -> Any:
        # Agent exits without writing anything new.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "noop {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "no_trajectory" in out
    # The OLD trajectory's "passed" outcome must not leak into the report.


def test_batch_multiple_tasks_mixed_outcomes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "alpha")
    _write_task(tasks_dir, "beta")
    _write_task(tasks_dir, "gamma")

    def fake_agent(cmd: str) -> Any:
        if "alpha" in cmd:
            _write_trajectory(tmp_path, "alpha", "passed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "beta" in cmd:
            _write_trajectory(tmp_path, "beta", "failed", "verify failed: [...]")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        # gamma — agent times out
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert "alpha" in out and "passed" in out
    assert "beta" in out and "failed" in out
    assert "gamma" in out and "agent_timeout" in out
    assert "Batch summary: 3 tasks" in out
    assert "pass-rate:     33%" in out
    assert rc == 1


def test_batch_task_filter_runs_only_that_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "alpha")
    _write_task(tasks_dir, "beta")

    called: list[str] = []

    def fake_agent(cmd: str) -> Any:
        called.append(cmd)
        for t in ("alpha", "beta"):
            if t in cmd:
                _write_trajectory(tmp_path, t, "passed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--task",
            "alpha",
            "--root",
            str(tmp_path),
        ]
    )
    assert len(called) == 1
    assert "alpha" in called[0]
    assert "beta" not in called[0]


def test_batch_missing_tasks_dir_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "batch",
            str(tmp_path / "nope"),
            "--agent",
            "x {task_id}",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_batch_no_tasks_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["batch", str(empty), "--agent", "x {task_id}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no tasks" in out
