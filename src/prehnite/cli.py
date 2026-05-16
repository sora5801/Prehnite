"""Tiny CLI for headless task runs, trajectory inspection, corpus stats,
and batch driving.

Four subcommands:

- `prehnite run <task.yaml>` — runs a task headless (smoke-test driver),
  optionally with a fixed list of agent commands via `--cmd`.
- `prehnite inspect <trajectory.jsonl>` — pretty-prints a captured
  trajectory so you can scan a run without manually parsing JSONL.
- `prehnite stats [<dir>]` — aggregates across every trajectory under a
  directory: per-task pass rate, failure reasons, egress summary.
- `prehnite batch <tasks-dir> --agent <cmd>` — for each discovered task,
  invokes the agent command (with `{task_id}` substituted) and reports
  the resulting trajectory's outcome. Sequential; one container at a
  time.

Use `run` to confirm your Docker image, task YAML, and trajectory wiring
hang together before pointing a real agent at the MCP server. Use
`inspect` to read what an agent (or the headless run) actually did. Use
`stats` to see the shape of a whole corpus of runs. Use `batch` to drive
an entire eval suite end-to-end.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import textwrap
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from prehnite.runner import run
from prehnite.schemas import EventType, RunStatus, TrajectoryEvent
from prehnite.tasks.loader import discover_tasks, load_task
from prehnite.trajectory import read_trajectory

EVENT_TYPES: list[str] = list(get_args(EventType))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prehnite")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser(
        "run", help="Run a task headless and print the result"
    )
    run_p.add_argument("task", type=Path, help="Path to a task YAML file")
    run_p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root for trajectory output (default: cwd)",
    )
    run_p.add_argument(
        "--cmd",
        action="append",
        default=[],
        metavar="CMD",
        help="Shell command to execute in the sandbox (repeatable)",
    )
    run_p.set_defaults(func=_cmd_run)

    inspect_p = sub.add_parser(
        "inspect",
        help="Pretty-print a trajectory JSONL file",
    )
    inspect_p.add_argument(
        "path", type=Path, help="Path to a trajectory .jsonl"
    )
    inspect_p.add_argument(
        "--full",
        action="store_true",
        help="Don't truncate cmd / stdout / stderr",
    )
    inspect_p.add_argument(
        "--type",
        action="append",
        choices=EVENT_TYPES,
        metavar="TYPE",
        help="Show only this event type (repeatable). Choices: "
        + ", ".join(EVENT_TYPES),
    )
    inspect_p.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip the summary at the end",
    )
    inspect_p.add_argument(
        "--summary-only",
        action="store_true",
        help="Show only the summary, no events",
    )
    inspect_p.set_defaults(func=_cmd_inspect)

    stats_p = sub.add_parser(
        "stats",
        help="Aggregate stats across a directory of trajectories",
    )
    stats_p.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("trajectories"),
        help="Directory to scan recursively for .jsonl trajectory files "
        "(default: trajectories/)",
    )
    stats_p.add_argument(
        "--task",
        metavar="TASK_ID",
        help="Filter to one task id (still shows the per-task row, just with one entry)",
    )
    stats_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable table",
    )
    stats_p.set_defaults(func=_cmd_stats)

    batch_p = sub.add_parser(
        "batch",
        help="Drive an agent against every task under a directory",
    )
    batch_p.add_argument(
        "tasks_dir",
        type=Path,
        help="Directory of task YAML files (scanned recursively)",
    )
    batch_p.add_argument(
        "--agent",
        required=True,
        metavar="CMD",
        help='Shell command template for the agent; "{task_id}" is '
        'substituted with each task id, e.g. '
        "'claude -p \"drive prehnite {task_id}\"'",
    )
    batch_p.add_argument(
        "--per-task-timeout",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Kill the agent subprocess after this many seconds (default: 300)",
    )
    batch_p.add_argument(
        "--task",
        metavar="TASK_ID",
        help="Filter to one task id (useful for debugging a single agent run)",
    )
    batch_p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root where trajectories are written (default: cwd)",
    )
    batch_p.set_defaults(func=_cmd_batch)

    args = parser.parse_args(argv)
    return int(args.func(args))


# --- subcommand: run ----------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    task = load_task(args.task)
    result = run(task, root=args.root, agent_commands=args.cmd or None)

    print(f"task:        {result.task_id}")
    print(f"status:      {result.status.value}")
    print(f"reason:      {result.reason}")
    print(f"trajectory:  {result.trajectory_path}")
    print(f"container:   {result.container_id}")

    return 0 if result.status is RunStatus.PASSED else 1


# --- subcommand: inspect ------------------------------------------------


def _cmd_inspect(args: argparse.Namespace) -> int:
    path: Path = args.path
    if not path.is_file():
        print(f"error: trajectory not found: {path}", file=sys.stderr)
        return 2

    events = read_trajectory(path)
    type_filter: set[str] = set(args.type or [])

    if not args.summary_only:
        for e in events:
            if type_filter and e.type not in type_filter:
                continue
            _format_event(e, full=args.full)

    if not args.no_summary:
        _format_summary(events)

    return 0


def _format_event(e: TrajectoryEvent, *, full: bool) -> None:
    """Render one event to stdout. ASCII only so Windows cmd.exe behaves."""
    ts = e.ts[11:19] if len(e.ts) >= 19 else e.ts  # HH:MM:SS from ISO-8601
    seq = e.seq
    d = e.data

    if e.type == "run_started":
        net = d.get("network")
        net_disp = net.get("mode", net) if isinstance(net, dict) else net
        print(
            f"[{seq:>3}] {ts} run_started      "
            f"task={d.get('task_id')}  image={d.get('image')}  "
            f"network={net_disp}"
        )
        return

    if e.type in ("setup_command", "agent_command", "verify_command"):
        dur = d.get("duration_ms", 0)
        cmd_raw = str(d.get("cmd", ""))
        cmd_disp = cmd_raw.replace("\n", "\\n")
        if not full:
            cmd_disp = _truncate(cmd_disp, 80)
        exit_code = d.get("exit_code", "?")
        print(
            f"[{seq:>3}] {ts} {e.type:<15}  ({dur}ms)  "
            f"{cmd_disp}  -> exit {exit_code}"
        )
        for name in ("stdout", "stderr"):
            val = str(d.get(name, ""))
            if not val:
                continue
            disp = val.replace("\n", "\\n")
            if not full:
                disp = _truncate(disp, 200)
            print(f"        {name}: {disp}")
        return

    if e.type == "agent_thought":
        thought = str(d.get("thought", ""))
        print(f"[{seq:>3}] {ts} agent_thought")
        if full:
            wrap_lines = thought.splitlines() or [""]
        else:
            wrap_lines = textwrap.wrap(thought, width=78) or [""]
        for ln in wrap_lines:
            print(f"        | {ln}")
        return

    if e.type == "egress_attempt":
        verdict = "ALLOWED" if d.get("allowed") else "DENIED "
        host = d.get("host")
        port = d.get("port")
        dur = d.get("duration_ms", 0)
        reason = d.get("reason", "")
        print(
            f"[{seq:>3}] {ts} egress_attempt   {verdict}  {host}:{port}"
            f"  ({dur}ms)  {reason}"
        )
        return

    if e.type == "run_finished":
        result = d.get("result", "?")
        reason = d.get("reason", "")
        print(f"[{seq:>3}] {ts} run_finished     {result}: {reason}")
        return

    print(f"[{seq:>3}] {ts} {e.type}: {d}")


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _format_summary(events: list[TrajectoryEvent]) -> None:
    print()
    if not events:
        print("  (empty trajectory)")
        return

    counts: dict[str, int] = {}
    for e in events:
        counts[e.type] = counts.get(e.type, 0) + 1

    final = events[-1]
    if final.type == "run_finished":
        result = str(final.data.get("result", "?"))
        reason = str(final.data.get("reason", ""))
        status_line = f"{result}: {reason}"
    else:
        status_line = "(incomplete - no run_finished event)"

    parts = [f"{count} {t}" for t, count in sorted(counts.items())]
    print(f"  {status_line}")
    print(f"  {', '.join(parts)}")


# --- subcommand: stats --------------------------------------------------


def _cmd_stats(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.exists():
        print(f"error: not found: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    files = sorted(root.rglob("*.jsonl"))
    if not files:
        if args.json:
            print(json.dumps(_empty_stats_payload(), indent=2))
        else:
            print(f"no .jsonl files found under {root}")
        return 0

    runs: list[_RunSummary] = []
    for f in files:
        try:
            events = read_trajectory(f)
        except Exception as e:  # malformed file; warn and keep going
            print(f"warning: skipping {f}: {e}", file=sys.stderr)
            continue
        summary = _summarize(f, events)
        if args.task and summary.task_id != args.task:
            continue
        runs.append(summary)

    if not runs:
        if args.json:
            # Stable empty shape so downstream tools don't have to special-case.
            print(json.dumps(_empty_stats_payload(), indent=2))
        else:
            print("no trajectories matched.")
        return 0

    if args.json:
        _print_stats_json(runs)
    else:
        _print_stats(runs)
    return 0


class _RunSummary:
    """Per-trajectory aggregate. One trajectory file collapses to this set
    of numbers; the table + JSON formatters work from a list of them."""

    __slots__ = (
        "path",
        "task_id",
        "outcome",
        "reason",
        "egress",
        "agent_cmd_count",
        "thought_count",
        "duration_s",
    )

    def __init__(
        self,
        path: Path,
        task_id: str | None,
        outcome: str,
        reason: str,
        egress: list[tuple[str, int, bool]],
        agent_cmd_count: int,
        thought_count: int,
        duration_s: float | None,
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.outcome = outcome  # passed | failed | error | incomplete
        self.reason = reason
        self.egress = egress
        self.agent_cmd_count = agent_cmd_count
        self.thought_count = thought_count
        self.duration_s = duration_s


def _parse_ts(ts: str) -> datetime | None:
    """Parse the trajectory's ISO-8601 timestamps (with trailing 'Z') across
    Python versions where fromisoformat may or may not accept 'Z'."""
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.fromisoformat(s)
    except (ValueError, IndexError):
        return None


def _summarize(path: Path, events: list[TrajectoryEvent]) -> _RunSummary:
    task_id: str | None = None
    outcome = "incomplete"
    reason = ""
    egress: list[tuple[str, int, bool]] = []
    agent_cmd_count = 0
    thought_count = 0
    start_ts: str | None = None
    end_ts: str | None = None

    for e in events:
        if e.type == "run_started":
            tid = e.data.get("task_id")
            if isinstance(tid, str):
                task_id = tid
            start_ts = e.ts
        elif e.type == "agent_command":
            agent_cmd_count += 1
        elif e.type == "agent_thought":
            thought_count += 1
        elif e.type == "egress_attempt":
            host = str(e.data.get("host", ""))
            port_raw = e.data.get("port", 0)
            port = int(port_raw) if isinstance(port_raw, (int, float, str)) else 0
            allowed = bool(e.data.get("allowed", False))
            egress.append((host, port, allowed))
        elif e.type == "run_finished":
            outcome = str(e.data.get("result", "incomplete"))
            reason = str(e.data.get("reason", ""))
            end_ts = e.ts

    duration_s: float | None = None
    if start_ts is not None and end_ts is not None:
        a, b = _parse_ts(start_ts), _parse_ts(end_ts)
        if a is not None and b is not None:
            duration_s = (b - a).total_seconds()

    return _RunSummary(
        path=path,
        task_id=task_id,
        outcome=outcome,
        reason=reason,
        egress=egress,
        agent_cmd_count=agent_cmd_count,
        thought_count=thought_count,
        duration_s=duration_s,
    )


def _per_task_rows(runs: list[_RunSummary]) -> list[dict[str, Any]]:
    """Build one row of stats per task_id, sorted by task_id."""
    by_task: dict[str, list[_RunSummary]] = {}
    for r in runs:
        by_task.setdefault(r.task_id or "(unknown)", []).append(r)

    rows: list[dict[str, Any]] = []
    for tid in sorted(by_task):
        rs = by_task[tid]
        total = len(rs)
        passed = sum(1 for r in rs if r.outcome == "passed")
        failed = sum(1 for r in rs if r.outcome == "failed")
        errored = sum(1 for r in rs if r.outcome == "error")

        cmd_counts = [r.agent_cmd_count for r in rs]
        med_cmds = float(statistics.median(cmd_counts)) if cmd_counts else 0.0

        durations = [r.duration_s for r in rs if r.duration_s is not None]
        med_dur = float(statistics.median(durations)) if durations else 0.0

        runs_with_thought = sum(1 for r in rs if r.thought_count > 0)
        thoughts_pct = (runs_with_thought * 100) // total if total else 0
        pass_rate = (passed * 100) // total if total else 0

        rows.append(
            {
                "task_id": tid,
                "runs": total,
                "pass": passed,
                "fail": failed,
                "error": errored,
                "pass_rate": pass_rate,
                "median_agent_cmds": round(med_cmds, 1),
                "median_duration_s": round(med_dur, 1),
                "thoughts_pct": thoughts_pct,
            }
        )
    return rows


def _overall_metrics(runs: list[_RunSummary]) -> dict[str, Any]:
    total = len(runs)
    passed = sum(1 for r in runs if r.outcome == "passed")
    total_thoughts = sum(r.thought_count for r in runs)
    runs_with_thought = sum(1 for r in runs if r.thought_count > 0)
    total_egress = sum(len(r.egress) for r in runs)
    egress_allowed = sum(1 for r in runs for (_, _, a) in r.egress if a)
    egress_denied = total_egress - egress_allowed
    return {
        "total_runs": total,
        "total_passed": passed,
        "pass_rate": (passed * 100) // total if total else 0,
        "total_thoughts": total_thoughts,
        "runs_with_thoughts": runs_with_thought,
        "thoughts_pct": (runs_with_thought * 100) // total if total else 0,
        "total_egress": total_egress,
        "egress_allowed": egress_allowed,
        "egress_denied": egress_denied,
    }


def _top_failure_reasons(runs: list[_RunSummary], n: int = 5) -> list[dict[str, Any]]:
    c: Counter[str] = Counter(
        r.reason for r in runs if r.outcome != "passed" and r.reason
    )
    return [{"reason": reason, "count": count} for reason, count in c.most_common(n)]


def _empty_stats_payload() -> dict[str, Any]:
    return {
        "total_runs": 0,
        "total_passed": 0,
        "pass_rate": 0,
        "total_thoughts": 0,
        "runs_with_thoughts": 0,
        "thoughts_pct": 0,
        "total_egress": 0,
        "egress_allowed": 0,
        "egress_denied": 0,
        "top_failure_reasons": [],
        "by_task": [],
    }


def _print_stats(runs: list[_RunSummary]) -> None:
    rows = _per_task_rows(runs)
    overall = _overall_metrics(runs)

    task_ids = {r.task_id for r in runs if r.task_id is not None}
    print(f"{len(runs)} trajectories across {len(task_ids)} tasks")
    print()

    # --- outcome breakdown ------------------------------------------
    outcomes = Counter(r.outcome for r in runs)
    print("By outcome:")
    for name in ("passed", "failed", "error", "incomplete"):
        count = outcomes.get(name, 0)
        if count or name in ("passed", "failed"):
            print(f"  {name:<11} {count:>4}")
    print()

    # --- per-task table ---------------------------------------------
    name_w = max(len("task"), max(len(str(r["task_id"])) for r in rows))
    header = (
        f"  {'task':<{name_w}}  {'runs':>4}  {'pass':>4}  {'fail':>4}  "
        f"{'err':>3}  {'pass-rate':>9}  {'med-cmds':>8}  {'med-dur':>8}  "
        f"{'thoughts%':>9}"
    )
    print("By task:")
    print(header)
    for r in rows:
        rate = f"{r['pass_rate']}%"
        thoughts = f"{r['thoughts_pct']}%"
        med_dur = f"{r['median_duration_s']:.1f}s"
        med_cmds = f"{r['median_agent_cmds']:.1f}"
        print(
            f"  {str(r['task_id']):<{name_w}}  {r['runs']:>4}  "
            f"{r['pass']:>4}  {r['fail']:>4}  {r['error']:>3}  "
            f"{rate:>9}  {med_cmds:>8}  {med_dur:>8}  {thoughts:>9}"
        )
    print()

    # --- overall ----------------------------------------------------
    print("Overall:")
    print(
        f"  {overall['total_runs']} trajectories, "
        f"{overall['total_passed']} passed ({overall['pass_rate']}%)"
    )
    print(
        f"  {overall['total_thoughts']} agent_thought events across "
        f"{overall['runs_with_thoughts']} runs "
        f"({overall['thoughts_pct']}% of runs had at least one thought)"
    )
    if overall["total_egress"]:
        print(
            f"  {overall['total_egress']} egress_attempt events "
            f"({overall['egress_allowed']} allowed, "
            f"{overall['egress_denied']} denied)"
        )
    print()

    # --- top failure reasons ----------------------------------------
    failures = _top_failure_reasons(runs)
    if failures:
        print("Top failure reasons:")
        for entry in failures:
            print(f"  {entry['count']:>2}  {_truncate(str(entry['reason']), 100)}")


def _print_stats_json(runs: list[_RunSummary]) -> None:
    payload = {
        **_overall_metrics(runs),
        "top_failure_reasons": _top_failure_reasons(runs),
        "by_task": _per_task_rows(runs),
    }
    print(json.dumps(payload, indent=2))


# --- subcommand: batch --------------------------------------------------


# Outcomes the agent process layer adds on top of the trajectory's own
# (passed | failed | error | incomplete). Kept short for table rendering.
_AGENT_TIMEOUT = "agent_timeout"
_NO_TRAJECTORY = "no_trajectory"


def _cmd_batch(args: argparse.Namespace) -> int:
    tasks_dir: Path = args.tasks_dir
    if not tasks_dir.is_dir():
        print(f"error: tasks dir not found: {tasks_dir}", file=sys.stderr)
        return 2

    try:
        tasks = discover_tasks(tasks_dir)
    except Exception as e:
        print(f"error: could not load tasks from {tasks_dir}: {e}", file=sys.stderr)
        return 2

    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
    if not tasks:
        print("no tasks to run.")
        return 0

    root: Path = args.root
    timeout: int = args.per_task_timeout

    results: list[tuple[str, str, float, Path | None]] = []
    n = len(tasks)
    width = max(len(t.id) for t in tasks)

    for i, task in enumerate(tasks, 1):
        agent_cmd = args.agent.replace("{task_id}", task.id)
        print(
            f"[{i:>{len(str(n))}}/{n}] {task.id:<{width}}  ... ",
            end="",
            flush=True,
        )

        started_at = time.time()
        start_mono = time.monotonic()
        outcome, traj = _run_one(agent_cmd, task.id, root, timeout, started_at)
        elapsed = time.monotonic() - start_mono

        traj_disp = (
            f"  {traj.relative_to(root) if root in traj.parents else traj}"
            if traj
            else ""
        )
        print(f"{outcome:<14} ({elapsed:>4.0f}s){traj_disp}")
        results.append((task.id, outcome, elapsed, traj))

    _print_batch_summary(results)

    # Exit 0 only if every task passed. Anything else is a non-success run
    # that a CI / eval pipeline should notice.
    return 0 if all(o == "passed" for _, o, _, _ in results) else 1


def _run_one(
    agent_cmd: str,
    task_id: str,
    root: Path,
    timeout: int,
    started_at: float,
) -> tuple[str, Path | None]:
    """Run the agent subprocess for one task; return (outcome, trajectory_path)."""
    try:
        subprocess.run(
            agent_cmd,
            shell=True,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        traj = _latest_new_trajectory(task_id, root, started_at)
        # The MCP server may have written a partial trajectory before the
        # agent was killed; surface it if so but mark the outcome as the
        # agent timeout, not whatever the partial run_finished says.
        return _AGENT_TIMEOUT, traj
    except FileNotFoundError as e:
        # Shell wasn't found, or on Windows the entry binary doesn't exist.
        print(f"\n  error: could not start agent: {e}", file=sys.stderr)
        return _AGENT_TIMEOUT, None

    traj = _latest_new_trajectory(task_id, root, started_at)
    if traj is None:
        return _NO_TRAJECTORY, None

    return _outcome_from_trajectory(traj), traj


def _latest_new_trajectory(
    task_id: str, root: Path, since_wallclock: float
) -> Path | None:
    """Return the newest .jsonl under trajectories/<task_id>/ written at or
    after `since_wallclock` (a `time.time()` value). None if no such file."""
    task_dir = root / "trajectories" / task_id
    if not task_dir.is_dir():
        return None
    new_files = [
        f for f in task_dir.glob("*.jsonl") if f.stat().st_mtime >= since_wallclock
    ]
    if not new_files:
        return None
    return max(new_files, key=lambda f: f.stat().st_mtime)


def _outcome_from_trajectory(path: Path) -> str:
    """Read the trajectory's run_finished event; default 'incomplete' if missing."""
    try:
        events = read_trajectory(path)
    except Exception:
        return "incomplete"
    if not events:
        return "incomplete"
    last = events[-1]
    if last.type == "run_finished":
        result = last.data.get("result", "incomplete")
        return str(result)
    return "incomplete"


def _print_batch_summary(
    results: list[tuple[str, str, float, Path | None]],
) -> None:
    if not results:
        return
    counts = Counter(outcome for _, outcome, _, _ in results)
    n = len(results)
    passed = counts.get("passed", 0)
    print()
    print(f"Batch summary: {n} tasks")
    for name in (
        "passed",
        "failed",
        "error",
        "incomplete",
        _AGENT_TIMEOUT,
        _NO_TRAJECTORY,
    ):
        if counts.get(name):
            print(f"  {name:<14} {counts[name]:>3}")
    pct = (passed * 100) // n if n else 0
    print(f"  pass-rate:     {pct}%")


if __name__ == "__main__":
    sys.exit(main())
