"""Tests for `prehnite stats <dir>` — corpus-level trajectory aggregation."""

from __future__ import annotations

import json
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
    # New format: single overall line under Overall: with the allowed/denied split.
    assert "2 egress_attempt events (1 allowed, 1 denied)" in out


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


# --- new richer metrics (median cmds, median duration, thoughts%, json) -----


def _write_raw_trajectory(
    path: Path, events: list[tuple[str, dict[str, object], str]]
) -> None:
    """Write a trajectory JSONL by hand. Lets tests control `ts` precisely
    (needed for duration-median assertions) and skip TrajectoryWriter's
    auto-stamping with the wall clock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for seq, (event_type, data, ts) in enumerate(events):
            f.write(
                json.dumps(
                    {"seq": seq, "ts": ts, "type": event_type, "data": data}
                )
                + "\n"
            )


def _ts(secs_after_zero: int) -> str:
    return f"2026-05-16T00:00:{secs_after_zero:02d}Z"


def _run_events(
    task_id: str,
    *,
    duration_s: int,
    n_agent_cmds: int,
    n_thoughts: int,
    outcome: str = "passed",
) -> list[tuple[str, dict[str, object], str]]:
    """Build a synthetic event sequence with the requested cardinalities."""
    events: list[tuple[str, dict[str, object], str]] = []
    events.append(
        ("run_started", {"task_id": task_id, "image": "i", "container_id": "c"}, _ts(0))
    )
    for i in range(n_agent_cmds):
        events.append(
            (
                "agent_command",
                {
                    "cmd": f"echo {i}",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "duration_ms": 1,
                },
                _ts(1 + i),
            )
        )
    for i in range(n_thoughts):
        events.append(("agent_thought", {"thought": f"step {i}"}, _ts(20 + i)))
    events.append(("run_finished", {"result": outcome, "reason": "ok"}, _ts(duration_s)))
    return events


def test_stats_median_agent_cmds_per_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two runs with 2 and 4 agent commands → median = 3.0
    _write_raw_trajectory(
        tmp_path / "demo" / "a.jsonl",
        _run_events("demo", duration_s=10, n_agent_cmds=2, n_thoughts=0),
    )
    _write_raw_trajectory(
        tmp_path / "demo" / "b.jsonl",
        _run_events("demo", duration_s=10, n_agent_cmds=4, n_thoughts=0),
    )
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    demo = next(ln for ln in out.splitlines() if "demo" in ln and "%" in ln)
    assert "3.0" in demo  # median agent cmds


def test_stats_median_duration_s_per_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two runs of duration 5s and 15s → median = 10.0s
    _write_raw_trajectory(
        tmp_path / "demo" / "a.jsonl",
        _run_events("demo", duration_s=5, n_agent_cmds=1, n_thoughts=0),
    )
    _write_raw_trajectory(
        tmp_path / "demo" / "b.jsonl",
        _run_events("demo", duration_s=15, n_agent_cmds=1, n_thoughts=0),
    )
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    demo = next(ln for ln in out.splitlines() if "demo" in ln and "%" in ln)
    assert "10.0s" in demo


def test_stats_thoughts_pct_per_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2 of 4 runs have at least one thought → thoughts_pct = 50%.
    for i, n in enumerate([2, 1, 0, 0]):
        _write_raw_trajectory(
            tmp_path / "demo" / f"{i}.jsonl",
            _run_events("demo", duration_s=1, n_agent_cmds=1, n_thoughts=n),
        )
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    demo = next(ln for ln in out.splitlines() if "demo" in ln and "%" in ln)
    # The per-task row ends with the thoughts% — assert the literal "50%" appears.
    assert "50%" in demo


def test_stats_overall_summary_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two tasks, three runs, varied thoughts.
    _write_raw_trajectory(
        tmp_path / "alpha" / "a.jsonl",
        _run_events("alpha", duration_s=10, n_agent_cmds=2, n_thoughts=1),
    )
    _write_raw_trajectory(
        tmp_path / "alpha" / "b.jsonl",
        _run_events("alpha", duration_s=10, n_agent_cmds=2, n_thoughts=0),
    )
    _write_raw_trajectory(
        tmp_path / "beta" / "a.jsonl",
        _run_events("beta", duration_s=20, n_agent_cmds=3, n_thoughts=2),
    )
    main(["stats", str(tmp_path)])
    out = capsys.readouterr().out
    assert "3 trajectories, 3 passed (100%)" in out
    # Thoughts: 1 + 0 + 2 = 3 events; runs with any thought = 2 of 3 = 66%.
    assert "3 agent_thought events across 2 runs (66% of runs had at least one thought)" in out


def test_stats_skips_malformed_files_with_stderr_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Write one good file and one malformed file in the same dir.
    _write_raw_trajectory(
        tmp_path / "demo" / "good.jsonl",
        _run_events("demo", duration_s=5, n_agent_cmds=1, n_thoughts=0),
    )
    bad = tmp_path / "demo" / "bad.jsonl"
    bad.write_text("not json at all\n", encoding="utf-8")

    rc = main(["stats", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    # The malformed file's warning lands on stderr; the good run is counted.
    assert "bad.jsonl" in captured.err
    assert "warning" in captured.err.lower()
    assert "1 trajectories across 1 tasks" in captured.out


def test_stats_json_output_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_raw_trajectory(
        tmp_path / "demo" / "a.jsonl",
        _run_events("demo", duration_s=10, n_agent_cmds=2, n_thoughts=1),
    )
    _write_raw_trajectory(
        tmp_path / "demo" / "b.jsonl",
        _run_events("demo", duration_s=20, n_agent_cmds=4, n_thoughts=0, outcome="failed"),
    )
    rc = main(["stats", str(tmp_path), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert payload["total_runs"] == 2
    assert payload["total_passed"] == 1
    assert payload["pass_rate"] == 50
    assert payload["total_thoughts"] == 1
    assert payload["runs_with_thoughts"] == 1
    assert payload["thoughts_pct"] == 50
    assert payload["total_egress"] == 0
    assert payload["by_task"][0]["task_id"] == "demo"
    assert payload["by_task"][0]["median_agent_cmds"] == 3.0
    assert payload["by_task"][0]["median_duration_s"] == 15.0
    assert payload["by_task"][0]["pass_rate"] == 50
    assert payload["by_task"][0]["thoughts_pct"] == 50


def test_stats_json_output_empty_dir_has_stable_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Downstream tools shouldn't need to special-case the empty case."""
    rc = main(["stats", str(tmp_path), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["total_runs"] == 0
    assert payload["by_task"] == []
    assert payload["top_failure_reasons"] == []
