"""Tests for `prehnite compare <A> <B>`.

The diff is exercised in three input modes: dir vs dir, JSON vs JSON, and
mixed (one of each). The same underlying summarization runs for the dir
case (re-used from `stats`), so the test fixtures write either small
JSONL trajectories or small `stats --json`-shaped files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prehnite.cli import main
from prehnite.trajectory import TrajectoryWriter


def _write_run(
    snapshot_dir: Path, task_id: str, outcome: str, filename: str | None = None
) -> None:
    """Write one synthetic trajectory under <snapshot_dir>/<task_id>/."""
    name = filename or f"{task_id}_{outcome}.jsonl"
    out = snapshot_dir / "trajectories" / task_id / name
    with TrajectoryWriter(out) as w:
        w.write(
            "run_started",
            {"task_id": task_id, "image": "i", "container_id": "c"},
        )
        w.write("run_finished", {"result": outcome, "reason": "ok"})


def _stats_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    """Build a payload matching `stats --json` shape from per-task rows."""
    total_runs = sum(int(r["runs"]) for r in rows)
    total_passed = sum(int(r["pass"]) for r in rows)
    pass_rate = (total_passed * 100) // total_runs if total_runs else 0
    return {
        "total_runs": total_runs,
        "total_passed": total_passed,
        "pass_rate": pass_rate,
        "by_task": rows,
    }


def _row(task_id: str, runs: int, passed: int, pass_rate: int | None = None) -> dict[str, object]:
    if pass_rate is None:
        pass_rate = (passed * 100) // runs if runs else 0
    return {
        "task_id": task_id,
        "runs": runs,
        "pass": passed,
        "fail": runs - passed,
        "error": 0,
        "pass_rate": pass_rate,
        "median_agent_cmds": 1.0,
        "median_duration_s": 1.0,
        "thoughts_pct": 0,
    }


# --- regression / improvement detection ---------------------------------


def test_compare_dirs_detects_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    # task `demo`: A is 1/1 passed (100%), B is 0/1 (0%) — regression.
    _write_run(a, "demo", "passed")
    _write_run(b, "demo", "failed")

    rc = main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out

    assert "demo" in out
    assert "regression" in out
    # Delta column shows the percent drop.
    assert "-100%" in out
    # Regression in B → exit 1 so CI notices.
    assert rc == 1


def test_compare_dirs_detects_improvement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(a, "demo", "failed")
    _write_run(b, "demo", "passed")

    rc = main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out
    assert "demo" in out
    assert "improvement" in out
    assert "+100%" in out
    # No regression → exit 0.
    assert rc == 0


def test_compare_dirs_detects_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(a, "demo", "passed")
    _write_run(b, "demo", "passed")
    rc = main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out
    assert "unchanged" in out
    assert rc == 0


def test_compare_new_and_dropped_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    # `gone` is in A only, `arrived` is in B only.
    _write_run(a, "gone", "passed")
    _write_run(b, "arrived", "passed")

    rc = main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out
    # Distinct categorisations.
    assert "gone" in out and "dropped" in out
    assert "arrived" in out and "new" in out
    # Neither counts as a regression — exit 0.
    assert rc == 0


# --- JSON input mode ----------------------------------------------------


def test_compare_two_json_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(
        json.dumps(_stats_payload([_row("demo", runs=2, passed=2)])),
        encoding="utf-8",
    )
    b.write_text(
        json.dumps(_stats_payload([_row("demo", runs=2, passed=1)])),
        encoding="utf-8",
    )
    rc = main(["compare", str(a), str(b)])
    out = capsys.readouterr().out
    assert "regression" in out
    assert "-50%" in out
    assert rc == 1


def test_compare_mixed_file_and_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A is a stats --json file; B is a fresh trajectories dir. Common use
    case: save baseline JSON, then compare future runs."""
    a = tmp_path / "baseline.json"
    a.write_text(
        json.dumps(_stats_payload([_row("demo", runs=2, passed=2)])),
        encoding="utf-8",
    )
    b = tmp_path / "current"
    _write_run(b, "demo", "passed")
    _write_run(b, "demo", "failed", filename="second.jsonl")

    rc = main(["compare", str(a), str(b / "trajectories")])
    out = capsys.readouterr().out
    # Baseline: 100%; current dir: 1/2 = 50%; delta = -50% (regression).
    assert "regression" in out
    assert "-50%" in out
    assert rc == 1


# --- aggregate / summary lines ------------------------------------------


def test_compare_shows_overall_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(a, "alpha", "passed")
    _write_run(a, "beta", "passed")
    _write_run(b, "alpha", "passed")
    _write_run(b, "beta", "failed")
    main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out
    # Overall section reflects per-snapshot pass rate.
    assert "A: 2/2 (100%)" in out
    assert "B: 1/2 (50%)" in out
    assert "delta: -50%" in out


def test_compare_summarizes_task_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(a, "regressed", "passed")
    _write_run(b, "regressed", "failed")
    _write_run(a, "stayed", "passed")
    _write_run(b, "stayed", "passed")
    _write_run(b, "new_task", "passed")
    main(["compare", str(a / "trajectories"), str(b / "trajectories")])
    out = capsys.readouterr().out
    # Last line of the human output is a task-count summary.
    assert "1 regression" in out
    assert "1 unchanged" in out
    assert "1 new" in out


# --- --json output ------------------------------------------------------


def test_compare_json_output_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(a, "regressed", "passed")
    _write_run(b, "regressed", "failed")
    _write_run(a, "stayed", "passed")
    _write_run(b, "stayed", "passed")
    _write_run(b, "freshtask", "passed")

    rc = main(
        ["compare", str(a / "trajectories"), str(b / "trajectories"), "--json"]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["regressions"] == ["regressed"]
    assert payload["improvements"] == []
    assert payload["unchanged"] == ["stayed"]
    assert payload["new"] == ["freshtask"]
    assert payload["dropped"] == []
    assert payload["overall_delta_pass_rate"] in (
        # depending on rounding direction A=2/2=100, B=2/3=66 → -34
        -34,
        -33,
    )
    assert "by_task" in payload


# --- error paths --------------------------------------------------------


def test_compare_missing_input_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["compare", str(tmp_path / "nope.json"), str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
