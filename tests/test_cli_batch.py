"""Tests for `prehnite batch <tasks-dir> --agent <cmd>`.

We monkeypatch `subprocess.run` so each test gets to script the "agent"
deterministically — either writing a trajectory, hanging until timeout,
or exiting without producing any trajectory at all. No real subprocess
invocation, no Docker.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from prehnite import cli
from prehnite.cli import main
from prehnite.trajectory import TrajectoryWriter


# --- fixtures / helpers --------------------------------------------------


def _write_task(
    tasks_dir: Path,
    task_id: str,
    *,
    tags: list[str] | None = None,
    difficulty: str | None = None,
) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    body = [f"id: {task_id}", "description: x"]
    if tags is not None:
        body.append(f"tags: [{', '.join(tags)}]")
    if difficulty is not None:
        body.append(f"difficulty: {difficulty}")
    (tasks_dir / f"{task_id}.yaml").write_text(
        "\n".join(body) + "\n", encoding="utf-8"
    )


def _write_trajectory(
    root: Path,
    task_id: str,
    result: str,
    reason: str = "",
    *,
    filename: str | None = None,
) -> Path:
    name = filename or f"{task_id}_run.jsonl"
    out = root / "trajectories" / task_id / name
    with TrajectoryWriter(out) as w:
        w.write(
            "run_started",
            {"task_id": task_id, "image": "i", "container_id": "c"},
        )
        w.write("run_finished", {"result": result, "reason": reason})
    return out


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, fn: Callable[[str, dict[str, Any]], Any]
) -> None:
    """Replace subprocess.run with a wrapper that calls `fn(cmd, kwargs)`.
    The `fn` is the test's agent impersonator — total freedom over what
    it does (write a trajectory, raise TimeoutExpired, write to the log
    file, exit non-zero)."""

    def _fake_run(cmd: str, **kwargs: Any) -> Any:
        return fn(cmd, kwargs)

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)


def _ok_completed(returncode: int = 0) -> Any:
    return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")


# --- basic outcomes ------------------------------------------------------


def test_batch_records_passed_outcome_from_trajectory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        assert "demo" in cmd, "task id should be substituted into the cmd"
        _write_trajectory(tmp_path, "demo", "passed", "ok")
        return _ok_completed()

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
    assert "demo" in out and "passed" in out
    assert "pass-rate:     100%" in out
    assert rc == 0


def test_batch_records_failed_outcome_and_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        _write_trajectory(tmp_path, "demo", "failed", "verify failed: [...]")
        return _ok_completed()

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
    assert "failed:        demo" in out  # list of failed task ids in the aggregate
    assert rc == 1


def test_batch_marks_timeout_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "wedged")

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
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
    assert "timeout" in out
    assert rc == 1


def test_batch_marks_no_trajectory_when_agent_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "ghost")

    def fake_agent(_cmd: str, _kwargs: dict[str, Any]) -> Any:
        return _ok_completed()

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
    assert "no-trajectory" in out
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

    old = _write_trajectory(tmp_path, "demo", "passed", "old")
    os.utime(old, (1_000_000.0, 1_000_000.0))  # very old mtime

    def fake_agent(_cmd: str, _kwargs: dict[str, Any]) -> Any:
        return _ok_completed()

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
    assert "no-trajectory" in out
    # The OLD trajectory's "passed" outcome must not leak through.


def test_batch_multiple_tasks_mixed_outcomes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "alpha")
    _write_task(tasks_dir, "beta")
    _write_task(tasks_dir, "gamma")

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        if "alpha" in cmd:
            _write_trajectory(tmp_path, "alpha", "passed")
            return _ok_completed()
        if "beta" in cmd:
            _write_trajectory(tmp_path, "beta", "failed", "verify failed: [...]")
            return _ok_completed()
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
    assert "gamma" in out and "timeout" in out
    assert "Batch summary: 3 tasks" in out
    assert "pass-rate:     33%" in out
    assert "failed:        beta" in out
    assert rc == 1


# --- filter / skip behaviour --------------------------------------------


def test_batch_filter_tag_keeps_only_tagged_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "alpha", tags=["smoke", "fast"])
    _write_task(tasks_dir, "beta", tags=["slow"])

    called: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        called.append(cmd)
        for t in ("alpha", "beta"):
            if t in cmd:
                _write_trajectory(tmp_path, t, "passed")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--filter-tag",
            "smoke",
            "--root",
            str(tmp_path),
        ]
    )
    assert len(called) == 1
    assert "alpha" in called[0]
    assert "beta" not in called[0]


def test_batch_filter_difficulty_keeps_only_matching(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "easy_one", difficulty="easy")
    _write_task(tasks_dir, "hard_one", difficulty="hard")

    called: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        called.append(cmd)
        for t in ("easy_one", "hard_one"):
            if t in cmd:
                _write_trajectory(tmp_path, t, "passed")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--filter-difficulty",
            "easy",
            "--root",
            str(tmp_path),
        ]
    )
    assert len(called) == 1
    assert "easy_one" in called[0]


def test_batch_skip_if_passed_within_skips_recent_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passed trajectory within the window means the agent never runs."""
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    # Recent pass trajectory.
    recent = _write_trajectory(tmp_path, "demo", "passed", "ok", filename="recent.jsonl")
    os.utime(recent, (time.time() - 60, time.time() - 60))  # 1 minute ago

    called: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        called.append(cmd)
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--skip-if-passed-within",
            "1",  # 1 hour
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert called == []  # agent never invoked
    assert "skipped" in out
    # Skip counts as success.
    assert rc == 0


def test_batch_skip_if_passed_within_runs_on_failed_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent FAILED trajectory should NOT trigger skip — we want to retry."""
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    fail = _write_trajectory(tmp_path, "demo", "failed", "verify failed", filename="fail.jsonl")
    os.utime(fail, (time.time() - 60, time.time() - 60))

    called: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        called.append(cmd)
        _write_trajectory(tmp_path, "demo", "passed", "ok", filename="retry.jsonl")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--skip-if-passed-within",
            "1",
            "--root",
            str(tmp_path),
        ]
    )
    assert len(called) == 1


def test_batch_skip_if_passed_within_runs_on_stale_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passed trajectory OLDER than the window should NOT trigger skip."""
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    stale = _write_trajectory(tmp_path, "demo", "passed", "old pass", filename="stale.jsonl")
    # 25 hours ago, window is 1h
    os.utime(stale, (time.time() - 25 * 3600, time.time() - 25 * 3600))

    called: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        called.append(cmd)
        _write_trajectory(tmp_path, "demo", "passed", "fresh", filename="fresh.jsonl")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--skip-if-passed-within",
            "1",
            "--root",
            str(tmp_path),
        ]
    )
    assert len(called) == 1


# --- {tools} substitution + log file ------------------------------------


def test_batch_substitutes_tools_placeholder(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    seen: list[str] = []

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        seen.append(cmd)
        _write_trajectory(tmp_path, "demo", "passed")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "agent --allowed-tools {tools} --task {task_id}",
            "--root",
            str(tmp_path),
        ]
    )
    assert seen, "agent should have been called"
    assert "mcp__prehnite__list_tasks" in seen[0]
    assert "mcp__prehnite__note" in seen[0]
    assert "mcp__prehnite__exec" in seen[0]
    # The placeholder itself should be gone.
    assert "{tools}" not in seen[0]


def test_batch_creates_per_task_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "demo")

    captured_kwargs: dict[str, Any] = {}

    def fake_agent(cmd: str, kwargs: dict[str, Any]) -> Any:
        captured_kwargs.update(kwargs)
        _write_trajectory(tmp_path, "demo", "passed")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--root",
            str(tmp_path),
        ]
    )

    # Log file gets opened and passed as stdout=; stderr is redirected to stdout
    # so both streams land in the same per-task file.
    assert captured_kwargs.get("stderr") == subprocess.STDOUT
    log_dir = tmp_path / "batch-logs"
    assert log_dir.is_dir()
    log_files = list(log_dir.glob("demo-*.log"))
    assert len(log_files) == 1


# --- aggregate / JSON ---------------------------------------------------


def test_batch_json_aggregate_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, "alpha")
    _write_task(tasks_dir, "beta")

    def fake_agent(cmd: str, _kwargs: dict[str, Any]) -> Any:
        if "alpha" in cmd:
            _write_trajectory(tmp_path, "alpha", "passed")
        else:
            _write_trajectory(tmp_path, "beta", "failed", "v fail")
        return _ok_completed()

    _patch_subprocess(monkeypatch, fake_agent)

    rc = main(
        [
            "batch",
            str(tasks_dir),
            "--agent",
            "x {task_id}",
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert payload["total_tasks"] == 2
    assert payload["by_status"]["passed"] == 1
    assert payload["by_status"]["failed"] == 1
    assert payload["failed_task_ids"] == ["beta"]
    assert isinstance(payload["wall_clock_s"], float)
    assert len(payload["tasks"]) == 2


def test_batch_json_empty_dir_has_stable_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(
        ["batch", str(empty), "--agent", "x {task_id}", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_tasks"] == 0
    assert payload["tasks"] == []
    assert payload["failed_task_ids"] == []


# --- error paths --------------------------------------------------------


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
