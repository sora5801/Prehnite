"""Tests for `prehnite stats <dir>` — corpus-level trajectory aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest

from prehnite.cli import main
from prehnite.trajectory import TrajectoryWriter


def _write_run(
    path: Path,
    *,
    task_id: str,
    outcome: str,
    reason: str = "",
    egress: list[tuple[str, int, bool]] | None = None,
) -> None:
    """Write a minimal trajectory file: run_started, optional egress, run_finished."""
    with TrajectoryWriter(path) as w:
        w.write(
            "run_started",
            {
                "task_id": task_id,
                "image": "prehnite-base:latest",
                "container_id": "c",
                "network": {"mode": "none", "extra_allow": []},
            },
        )
        for host, port, allowed in egress or []:
            w.write(
                "egress_attempt",
                {
                    "host": host,
                    "port": port,
                    "allowed": allowed,
                    "reason": "matched allowlist" if allowed else "not in allowlist",
                    "duration_ms": 1,
                },
            )
        w.write("run_finished", {"result": outcome, "reason": reason})


def _build_corpus(root: Path) -> None:
    """Build a corpus with mixed outcomes/tasks/egress for cross-cutting assertions."""
    (root / "fix_off_by_one").mkdir()
    _write_run(
        root / "fix_off_by_one" / "a.jsonl",
        task_id="fix_off_by_one",
        outcome="passed",
        reason="all verify checks passed",
    )
    _write_run(
        root / "fix_off_by_one" / "b.jsonl",
        task_id="fix_off_by_one",
        outcome="passed",
        reason="all verify checks passed",
    )
    (root / "hello").mkdir()
    _write_run(
        root / "hello" / "a.jsonl",
        task_id="hello",
        outcome="failed",
        reason="no agent activity (verify ran on untouched workspace)",
    )
    _write_run(
        root / "hello" / "b.jsonl",
        task_id="hello",
        outcome="failed",
        reason="no agent activity (verify ran on untouched workspace)",
    )
    (root / "egress_allowlist").mkdir()
    _write_run(
        root / "egress_allowlist" / "a.jsonl",
        task_id="egress_allowlist",
        outcome="passed",
        reason="all verify checks passed",
        egress=[("example.com", 443, True), ("www.iana.org", 443, False)],
    )


def test_stats_reports_total_and_outcome_breakdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_corpus(tmp_path)
    rc = main(["stats", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "5 trajectories across 3 tasks" in out
    assert "passed" in out and "3" in out
    assert "failed" in out and "2" in out


def test_stats_per_task_pass_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_corpus(tmp_path)
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    # Per-task table: fix_off_by_one 2/2 = 100%, hello 0/2 = 0%
    lines = out.splitlines()
    foo = next(ln for ln in lines if "fix_off_by_one" in ln)
    assert "2" in foo and "100%" in foo
    hello = next(ln for ln in lines if "hello" in ln and "%" in ln)
    assert "0%" in hello


def test_stats_top_failure_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_corpus(tmp_path)
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Top failure reasons:" in out
    assert "no agent activity" in out
    # Two hello failures with the same reason — should count as 2 not 1.
    assert " 2  no agent activity" in out


def test_stats_egress_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_corpus(tmp_path)
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Egress (2 attempts across 1 runs):" in out
    assert "example.com:443" in out and "allowed" in out
    assert "www.iana.org:443" in out and "denied" in out


def test_stats_egress_section_omitted_when_no_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "only_local").mkdir()
    _write_run(
        tmp_path / "only_local" / "a.jsonl",
        task_id="only_local",
        outcome="passed",
        reason="all verify checks passed",
    )
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Egress" not in out


def test_stats_task_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_corpus(tmp_path)
    main(["stats", str(tmp_path), "--task", "fix_off_by_one"])
    out = capsys.readouterr().out
    assert "2 trajectories across 1 tasks" in out
    assert "fix_off_by_one" in out
    assert "hello" not in out


def test_stats_incomplete_run_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "broken").mkdir()
    p = tmp_path / "broken" / "a.jsonl"
    with TrajectoryWriter(p) as w:
        w.write(
            "run_started",
            {"task_id": "broken", "image": "i", "container_id": "c"},
        )
        # never write run_finished — simulates a crashed run
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    assert "incomplete" in out
    assert "1" in out


def test_stats_missing_dir_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["stats", str(tmp_path / "nope")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_stats_empty_dir_handled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["stats", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no .jsonl files" in out
