"""Tiny CLI for headless task runs, trajectory inspection, corpus stats,
batch driving, cross-snapshot comparison, and host-side cleanup.

Six subcommands:

- `prehnite run <task.yaml>` — runs a task headless (smoke-test driver),
  optionally with a fixed list of agent commands via `--cmd`.
- `prehnite inspect <trajectory.jsonl>` — pretty-prints a captured
  trajectory so you can scan a run without manually parsing JSONL.
- `prehnite stats [<dir>]` — aggregates across every trajectory under a
  directory: per-task pass rate, failure reasons, egress summary.
- `prehnite batch <tasks-dir> --agent <cmd>` — for each discovered task,
  invokes the agent command (with `{task_id}` / `{tools}` substituted)
  and reports the resulting trajectory's outcome.
- `prehnite compare <A> <B>` — diffs two snapshots (either trajectory
  directories or stats --json outputs) and flags per-task regressions /
  improvements / new / dropped.
- `prehnite reap` — removes orphan containers from crashed runs and
  old batch-logs files. Respects live MCP sessions.

Use `run` for smoke tests, `inspect` to read one run, `stats` for one
corpus, `batch` to drive an agent across a suite, `compare` to see how
today's runs differ from yesterday's, and `reap` to keep the host tidy.
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
        help='Shell command template for the agent. Placeholders: '
        '"{task_id}" is substituted per task, "{tools}" expands to the '
        'comma-separated list of mcp__prehnite__* tool names. Example: '
        '\'claude -p --allowed-tools {tools} --model sonnet '
        '"Drive prehnite {task_id} end-to-end and report the result"\'',
    )
    batch_p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root for trajectory output (default: cwd). Must match "
        "the MCP server's PREHNITE_ROOT.",
    )
    batch_p.add_argument(
        "--filter-tag",
        metavar="TAG",
        help="Pre-filter the task set: keep only tasks whose `tags` list "
        "contains this value.",
    )
    batch_p.add_argument(
        "--filter-difficulty",
        metavar="LEVEL",
        help="Pre-filter the task set: keep only tasks whose `difficulty` "
        "equals this value.",
    )
    batch_p.add_argument(
        "--skip-if-passed-within",
        type=float,
        default=0,
        metavar="HOURS",
        help="For each task, look at the newest existing trajectory; if it "
        "is passed and within this many hours, skip the run. Default 0 "
        "(run everything).",
    )
    batch_p.add_argument(
        "--per-task-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="Kill the agent subprocess after this many seconds (default: 600)",
    )
    batch_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the aggregate as JSON to stdout instead of the human table",
    )
    batch_p.set_defaults(func=_cmd_batch)

    compare_p = sub.add_parser(
        "compare",
        help="Diff two snapshots (trajectory dirs or stats --json files) "
        "and surface per-task regressions / improvements",
    )
    compare_p.add_argument(
        "a",
        type=Path,
        help="First snapshot: a trajectory directory OR a .json file "
        "produced by `prehnite stats --json`",
    )
    compare_p.add_argument(
        "b",
        type=Path,
        help="Second snapshot: same shape options as A",
    )
    compare_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the diff as JSON instead of the human table",
    )
    compare_p.set_defaults(func=_cmd_compare)

    reap_p = sub.add_parser(
        "reap",
        help="Remove orphan prehnite containers and stale batch-logs files",
    )
    reap_p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root (default: cwd). Used to find live sessions and "
        "batch-logs.",
    )
    reap_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without actually removing anything.",
    )
    reap_p.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        metavar="HOURS",
        help="batch-logs older than this many hours are removed "
        "(default: 24).",
    )
    reap_p.set_defaults(func=_cmd_reap)

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


# Status names the batch reports. Trajectory-derived: passed | failed | error
# | incomplete. Subprocess-derived: timeout | no-trajectory | skipped.
_TIMEOUT = "timeout"
_NO_TRAJECTORY = "no-trajectory"
_SKIPPED = "skipped"

# The MCP tools the prehnite server exposes. Kept here so `{tools}` in the
# agent template expands to the exact allowed-tools list a real agent driver
# (claude -p, etc.) needs. Keep in sync with mcp_server.build_server().
_MCP_TOOL_NAMES: tuple[str, ...] = (
    "list_tasks",
    "describe_task",
    "start_task",
    "exec",
    "note",
    "read_trajectory",
    "fork",
    "revert",
    "finish_task",
    "abort_task",
)


class _BatchResult:
    """One row of batch output. Carries everything both the table and the
    JSON aggregate need."""

    __slots__ = ("task_id", "status", "duration_s", "trajectory")

    def __init__(
        self,
        task_id: str,
        status: str,
        duration_s: float,
        trajectory: Path | None,
    ) -> None:
        self.task_id = task_id
        self.status = status
        self.duration_s = duration_s
        self.trajectory = trajectory


def _cmd_batch(args: argparse.Namespace) -> int:
    tasks_dir: Path = args.tasks_dir
    if not tasks_dir.is_dir():
        print(f"error: tasks dir not found: {tasks_dir}", file=sys.stderr)
        return 2

    try:
        tasks = list(discover_tasks(tasks_dir))
    except Exception as e:
        print(f"error: could not load tasks from {tasks_dir}: {e}", file=sys.stderr)
        return 2

    if args.filter_tag:
        tasks = [t for t in tasks if args.filter_tag in t.tags]
    if args.filter_difficulty:
        tasks = [t for t in tasks if t.difficulty == args.filter_difficulty]

    if not tasks:
        if args.json:
            print(json.dumps(_empty_batch_payload(), indent=2))
        else:
            print("no tasks to run.")
        return 0

    root: Path = args.root
    timeout: int = args.per_task_timeout
    skip_window_s = float(args.skip_if_passed_within) * 3600.0

    # `{tools}` substitution: do it once, since it's constant across tasks.
    tools_value = ",".join(f"mcp__prehnite__{name}" for name in _MCP_TOOL_NAMES)
    agent_tmpl = args.agent.replace("{tools}", tools_value)

    results: list[_BatchResult] = []
    n = len(tasks)
    name_w = max(len(t.id) for t in tasks)
    batch_started_mono = time.monotonic()

    for i, task in enumerate(tasks, 1):
        # --- skip-if-passed-within check first ----------------------
        recent = _recent_passed_trajectory(task.id, root, skip_window_s)
        if recent is not None:
            r = _BatchResult(task.id, _SKIPPED, 0.0, recent)
            results.append(r)
            if not args.json:
                _emit_row(i, n, name_w, r, root)
            continue

        # --- agent subprocess ---------------------------------------
        agent_cmd = agent_tmpl.replace("{task_id}", task.id)
        started_at = time.time()
        start_mono = time.monotonic()
        status, traj = _run_one(agent_cmd, task.id, root, timeout, started_at)
        elapsed = time.monotonic() - start_mono

        r = _BatchResult(task.id, status, elapsed, traj)
        results.append(r)
        if not args.json:
            _emit_row(i, n, name_w, r, root)

    wall_clock_s = time.monotonic() - batch_started_mono

    if args.json:
        print(json.dumps(_batch_payload(results, wall_clock_s), indent=2))
    else:
        _print_batch_aggregate(results, wall_clock_s)

    # Exit 0 iff every task either passed or was skipped (skipped tasks are
    # cached passes, by definition). Anything else is non-success — CI /
    # eval pipelines should notice.
    return 0 if all(r.status in ("passed", _SKIPPED) for r in results) else 1


def _emit_row(
    i: int, n: int, name_w: int, r: _BatchResult, root: Path
) -> None:
    """Per-task progress line. Spec format: [N/M] <task_id> <status> <dur>s <path>."""
    traj_disp = ""
    if r.trajectory is not None:
        try:
            traj_disp = str(r.trajectory.relative_to(root))
        except ValueError:
            traj_disp = str(r.trajectory)
    print(
        f"[{i:>{len(str(n))}}/{n}] {r.task_id:<{name_w}} "
        f"{r.status:<13} {r.duration_s:>4.0f}s {traj_disp}".rstrip(),
        flush=True,
    )


def _run_one(
    agent_cmd: str,
    task_id: str,
    root: Path,
    timeout: int,
    started_at: float,
) -> tuple[str, Path | None]:
    """Spawn one agent subprocess; return (status, trajectory_path).

    Subprocess stderr (and stdout, merged) lands in
    root/batch-logs/<task_id>-<UTC-stamp>.log so the agent's chatter is
    preserved for debugging but never lands on the batch caller's terminal.
    """
    log_path = _open_batch_log(root, task_id, started_at)
    log_fh: Any | None = None
    try:
        log_fh = log_path.open("a", encoding="utf-8")
        try:
            subprocess.run(
                agent_cmd,
                shell=True,
                timeout=timeout,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=root,
            )
        except subprocess.TimeoutExpired:
            traj = _latest_new_trajectory(task_id, root, started_at)
            return _TIMEOUT, traj
        except FileNotFoundError as e:
            print(f"\n  error: could not start agent: {e}", file=sys.stderr)
            return _TIMEOUT, None
    finally:
        if log_fh is not None:
            log_fh.close()

    traj = _latest_new_trajectory(task_id, root, started_at)
    if traj is None:
        return _NO_TRAJECTORY, None
    return _outcome_from_trajectory(traj), traj


def _open_batch_log(root: Path, task_id: str, started_at: float) -> Path:
    """Compute (and ensure the parent of) the per-task log file path."""
    log_dir = root / "batch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(started_at).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{task_id}-{stamp}.log"


def _latest_new_trajectory(
    task_id: str, root: Path, since_wallclock: float
) -> Path | None:
    """Return the newest .jsonl under trajectories/<task_id>/ written at or
    after `since_wallclock` (a `time.time()` value). None if no such file.

    Uses 1s of slop on the mtime comparison because Windows reports file
    mtime with lower precision than `time.time()` — a file written 0.5ms
    after `time.time()` can stat as having an mtime ~0.5ms *before* it.
    The pre-existing-trajectory test (test_batch_ignores_pre_existing_
    trajectories) uses mtime=1_000_000 (year 1970), so 1s of slop doesn't
    breach that contract.
    """
    task_dir = root / "trajectories" / task_id
    if not task_dir.is_dir():
        return None
    cutoff = since_wallclock - 1.0
    new_files = [
        f for f in task_dir.glob("*.jsonl") if f.stat().st_mtime >= cutoff
    ]
    if not new_files:
        return None
    return max(new_files, key=lambda f: f.stat().st_mtime)


def _recent_passed_trajectory(
    task_id: str, root: Path, window_s: float
) -> Path | None:
    """If skip-if-passed-within is in effect (window_s > 0), look at the
    newest trajectory for this task; if its mtime is within the window AND
    it ended in passed, return its path so the caller can skip the run."""
    if window_s <= 0:
        return None
    task_dir = root / "trajectories" / task_id
    if not task_dir.is_dir():
        return None
    files = sorted(
        task_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    if not files:
        return None
    newest = files[0]
    if time.time() - newest.stat().st_mtime > window_s:
        return None
    try:
        events = read_trajectory(newest)
    except Exception:
        return None
    if not events:
        return None
    last = events[-1]
    if last.type != "run_finished":
        return None
    if last.data.get("result") != "passed":
        return None
    return newest


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


def _print_batch_aggregate(
    results: list[_BatchResult], wall_clock_s: float
) -> None:
    print()
    print(f"Batch summary: {len(results)} tasks ({wall_clock_s:.0f}s wall clock)")
    counts = Counter(r.status for r in results)
    for name in (
        "passed",
        "failed",
        "error",
        "incomplete",
        _TIMEOUT,
        _NO_TRAJECTORY,
        _SKIPPED,
    ):
        if counts.get(name):
            print(f"  {name:<14} {counts[name]:>3}")
    n = len(results)
    if n:
        passed = counts.get("passed", 0) + counts.get(_SKIPPED, 0)
        pct = (passed * 100) // n
        print(f"  pass-rate:     {pct}%")
    failed_ids = [r.task_id for r in results if r.status == "failed"]
    if failed_ids:
        print(f"  failed:        {', '.join(failed_ids)}")


def _batch_payload(
    results: list[_BatchResult], wall_clock_s: float
) -> dict[str, Any]:
    counts = Counter(r.status for r in results)
    return {
        "total_tasks": len(results),
        "by_status": dict(counts),
        "wall_clock_s": round(wall_clock_s, 1),
        "failed_task_ids": [r.task_id for r in results if r.status == "failed"],
        "tasks": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "duration_s": round(r.duration_s, 1),
                "trajectory": (str(r.trajectory) if r.trajectory else None),
            }
            for r in results
        ],
    }


def _empty_batch_payload() -> dict[str, Any]:
    return {
        "total_tasks": 0,
        "by_status": {},
        "wall_clock_s": 0.0,
        "failed_task_ids": [],
        "tasks": [],
    }


# --- subcommand: compare ------------------------------------------------


# Per-task diff statuses. Order matters: regressions surface first.
_DIFF_STATUSES: tuple[str, ...] = (
    "regression",
    "improvement",
    "unchanged",
    "new",
    "dropped",
)


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        a_by_task, a_overall = _load_snapshot(args.a)
        b_by_task, b_overall = _load_snapshot(args.b)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    diffs = _diff_snapshots(a_by_task, b_by_task)

    if args.json:
        print(json.dumps(_compare_payload(diffs, a_overall, b_overall), indent=2))
    else:
        _print_diff(diffs, a_overall, b_overall)

    # Exit 1 iff there's at least one regression — designed so CI can wrap
    # `prehnite compare baseline.json trajectories/` and notice degradation.
    regressed = any(d["status"] == "regression" for d in diffs)
    return 1 if regressed else 0


def _load_snapshot(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load a snapshot from either a `stats --json` file or a trajectories
    directory. Returns (by_task_dict, overall_dict) — same shape regardless
    of input form so the diff doesn't care."""
    if not path.exists():
        raise FileNotFoundError(f"compare: input not found: {path}")

    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_task = {
            row["task_id"]: row for row in payload.get("by_task", [])
        }
        overall = {
            "total_runs": int(payload.get("total_runs", 0)),
            "total_passed": int(payload.get("total_passed", 0)),
            "pass_rate": int(payload.get("pass_rate", 0)),
        }
        return by_task, overall

    if path.is_dir():
        # Same summarization stats uses; this is the whole point of the
        # _per_task_rows / _overall_metrics extraction.
        runs: list[_RunSummary] = []
        for f in sorted(path.rglob("*.jsonl")):
            try:
                events = read_trajectory(f)
            except Exception as e:
                print(f"warning: skipping {f}: {e}", file=sys.stderr)
                continue
            runs.append(_summarize(f, events))
        rows = _per_task_rows(runs) if runs else []
        by_task = {row["task_id"]: row for row in rows}
        if runs:
            m = _overall_metrics(runs)
            overall = {
                "total_runs": m["total_runs"],
                "total_passed": m["total_passed"],
                "pass_rate": m["pass_rate"],
            }
        else:
            overall = {"total_runs": 0, "total_passed": 0, "pass_rate": 0}
        return by_task, overall

    raise FileNotFoundError(f"compare: not a file or directory: {path}")


def _diff_snapshots(
    a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-task diff. Categorises each task as regression / improvement /
    unchanged / new / dropped based on pass_rate movement."""
    all_tasks = sorted(set(a) | set(b))
    diffs: list[dict[str, Any]] = []
    for tid in all_tasks:
        a_row = a.get(tid)
        b_row = b.get(tid)
        if a_row is None and b_row is not None:
            diffs.append(
                {
                    "task_id": tid,
                    "status": "new",
                    "a": None,
                    "b": b_row,
                    "delta_pass_rate": None,
                }
            )
        elif b_row is None and a_row is not None:
            diffs.append(
                {
                    "task_id": tid,
                    "status": "dropped",
                    "a": a_row,
                    "b": None,
                    "delta_pass_rate": None,
                }
            )
        else:
            assert a_row is not None and b_row is not None
            delta = int(b_row.get("pass_rate", 0)) - int(a_row.get("pass_rate", 0))
            if delta < 0:
                status = "regression"
            elif delta > 0:
                status = "improvement"
            else:
                status = "unchanged"
            diffs.append(
                {
                    "task_id": tid,
                    "status": status,
                    "a": a_row,
                    "b": b_row,
                    "delta_pass_rate": delta,
                }
            )
    return diffs


def _print_diff(
    diffs: list[dict[str, Any]],
    a_overall: dict[str, Any],
    b_overall: dict[str, Any],
) -> None:
    if not diffs:
        print("no tasks in either snapshot.")
        return

    # Sort: regressions first (most actionable), then improvements,
    # unchanged, new, dropped.
    order = {s: i for i, s in enumerate(_DIFF_STATUSES)}
    diffs_sorted = sorted(
        diffs, key=lambda d: (order[str(d["status"])], str(d["task_id"]))
    )

    name_w = max(len("task"), max(len(str(d["task_id"])) for d in diffs_sorted))
    header = (
        f"  {'task':<{name_w}}  {'A':>11}  {'B':>11}  {'delta':>7}  status"
    )
    print(header)
    for d in diffs_sorted:
        a_disp = _fmt_rate_with_runs(d["a"])
        b_disp = _fmt_rate_with_runs(d["b"])
        if d["delta_pass_rate"] is None:
            delta_disp = "-"
        else:
            delta_disp = f"{d['delta_pass_rate']:+d}%"
        print(
            f"  {str(d['task_id']):<{name_w}}  {a_disp:>11}  {b_disp:>11}  "
            f"{delta_disp:>7}  {d['status']}"
        )

    print()
    a_rate = a_overall.get("pass_rate", 0)
    b_rate = b_overall.get("pass_rate", 0)
    a_runs = a_overall.get("total_runs", 0)
    b_runs = b_overall.get("total_runs", 0)
    a_passed = a_overall.get("total_passed", 0)
    b_passed = b_overall.get("total_passed", 0)
    print("Overall:")
    print(f"  A: {a_passed}/{a_runs} ({a_rate}%)")
    print(f"  B: {b_passed}/{b_runs} ({b_rate}%)")
    print(f"  delta: {b_rate - a_rate:+d}%")

    print()
    counts = Counter(str(d["status"]) for d in diffs)
    parts = [f"{counts.get(s, 0)} {s}" for s in _DIFF_STATUSES if counts.get(s)]
    print(f"Tasks: {', '.join(parts)}" if parts else "Tasks: (no differences)")


def _fmt_rate_with_runs(row: dict[str, Any] | None) -> str:
    if row is None:
        return "N/A"
    return f"{int(row.get('pass_rate', 0))}% ({int(row.get('runs', 0))})"


def _compare_payload(
    diffs: list[dict[str, Any]],
    a_overall: dict[str, Any],
    b_overall: dict[str, Any],
) -> dict[str, Any]:
    bucket: dict[str, list[str]] = {s: [] for s in _DIFF_STATUSES}
    for d in diffs:
        bucket[str(d["status"])].append(str(d["task_id"]))

    a_rate = int(a_overall.get("pass_rate", 0))
    b_rate = int(b_overall.get("pass_rate", 0))
    return {
        "a": a_overall,
        "b": b_overall,
        "overall_delta_pass_rate": b_rate - a_rate,
        "regressions": bucket["regression"],
        "improvements": bucket["improvement"],
        "unchanged": bucket["unchanged"],
        "new": bucket["new"],
        "dropped": bucket["dropped"],
        "by_task": diffs,
    }


# --- subcommand: reap ---------------------------------------------------


def _cmd_reap(args: argparse.Namespace) -> int:
    root: Path = args.root
    dry_run: bool = args.dry_run
    older_than_hours: float = args.older_than_hours

    # Late import: docker SDK only loads on demand so other subcommands
    # (run/inspect/stats/compare/batch) don't pay for it.
    try:
        import docker
        from docker.errors import APIError, DockerException, NotFound
    except ImportError:  # pragma: no cover — docker is a hard dep
        print("error: docker SDK not installed", file=sys.stderr)
        return 2

    try:
        client = docker.from_env()
    except DockerException as e:
        print(f"error: could not reach Docker daemon: {e}", file=sys.stderr)
        return 2

    live_session_ids = _live_session_ids(root)
    live_container_ids = _live_session_container_ids(root)
    candidates = _find_prehnite_containers(client)

    # A container is an orphan if it's not actively used by any session
    # descriptor in <root>/sessions/. Live containers stay put.
    orphans = [
        c for c in candidates
        if _short_id(c) not in live_container_ids and c.id not in live_container_ids
    ]
    # Snapshot images belonging to sessions that no longer exist (or never
    # existed). The `prehnite.session_id` label is set by the fork tool.
    orphan_snapshots = _find_orphan_snapshot_images(client, live_session_ids)
    stale_logs = _find_stale_logs(root, older_than_hours)

    _print_reap_plan(orphans, orphan_snapshots, stale_logs, root, dry_run)

    if dry_run:
        return 0

    reaped_containers = 0
    for c in orphans:
        try:
            c.remove(force=True)
            reaped_containers += 1
        except (APIError, NotFound) as e:
            print(f"  warning: could not remove {_short_id(c)}: {e}", file=sys.stderr)

    reaped_snapshots = 0
    for img in orphan_snapshots:
        try:
            client.images.remove(img.id, force=True)
            reaped_snapshots += 1
        except (APIError, NotFound) as e:
            print(f"  warning: could not remove snapshot {img.id}: {e}", file=sys.stderr)

    deleted_logs = 0
    for f in stale_logs:
        try:
            f.unlink()
            deleted_logs += 1
        except OSError as e:
            print(f"  warning: could not delete {f}: {e}", file=sys.stderr)

    print()
    print(
        f"Reaped {reaped_containers} containers, {reaped_snapshots} snapshots, "
        f"and {deleted_logs} batch logs."
    )
    return 0


def _live_session_container_ids(root: Path) -> set[str]:
    """Read every <root>/sessions/*.json descriptor; collect the
    container_ids the MCP server considers live. Anything else with the
    prehnite label is an orphan eligible for cleanup."""
    sdir = root / "sessions"
    if not sdir.is_dir():
        return set()
    ids: set[str] = set()
    for f in sdir.glob("*.json"):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cid = payload.get("container_id")
        if cid:
            ids.add(str(cid))
    return ids


def _live_session_ids(root: Path) -> set[str]:
    """Read every <root>/sessions/*.json descriptor; collect the session
    ids the MCP server considers live. Used to identify orphan snapshot
    images (those tagged with a `prehnite.session_id` label whose owning
    session is gone)."""
    sdir = root / "sessions"
    if not sdir.is_dir():
        return set()
    ids: set[str] = set()
    for f in sdir.glob("*.json"):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = payload.get("session_id")
        if sid:
            ids.add(str(sid))
    return ids


def _find_orphan_snapshot_images(
    client: Any, live_session_ids: set[str]
) -> list[Any]:
    """Snapshot images (labeled `prehnite.snapshot=true`) whose session
    is no longer live. The session id comes from the
    `prehnite.session_id` label attached at commit time."""
    try:
        candidates = client.images.list(
            filters={"label": "prehnite.snapshot=true"}
        )
    except Exception as e:
        print(f"warning: snapshot image listing failed: {e}", file=sys.stderr)
        return []
    orphans: list[Any] = []
    for img in candidates:
        labels = _image_labels(img)
        sid = labels.get("prehnite.session_id")
        # No session label = can't tell which session owns it; treat as
        # orphan (probably a forgotten test or manual `docker commit`).
        # Snapshots labeled with a session id whose session is gone:
        # likewise orphan.
        if not sid or sid not in live_session_ids:
            orphans.append(img)
    return orphans


def _image_labels(image: Any) -> dict[str, str]:
    try:
        attrs = dict(image.attrs or {})
    except Exception:
        attrs = {}
    config = attrs.get("Config", {}) or {}
    labels = config.get("Labels") or {}
    return {str(k): str(v) for k, v in labels.items()}


def _find_prehnite_containers(client: Any) -> list[Any]:
    """Two queries de-duped: containers with the `prehnite=true` label
    (everything from this version onward) plus containers whose image is
    `prehnite-base:*` (catches pre-label-era orphans the user wants to
    sweep up). Includes stopped/exited containers since those are the
    main reaping target."""
    seen: dict[str, Any] = {}
    try:
        for c in client.containers.list(all=True, filters={"label": "prehnite=true"}):
            seen[str(c.id)] = c
    except Exception as e:  # docker SDK is noisy across versions
        print(f"warning: label filter failed: {e}", file=sys.stderr)

    try:
        for c in client.containers.list(all=True, filters={"ancestor": "prehnite-base"}):
            seen[str(c.id)] = c
    except Exception as e:
        print(f"warning: ancestor filter failed: {e}", file=sys.stderr)

    return list(seen.values())


def _find_stale_logs(root: Path, older_than_hours: float) -> list[Path]:
    log_dir = root / "batch-logs"
    if not log_dir.is_dir():
        return []
    cutoff = time.time() - older_than_hours * 3600
    return sorted(
        f for f in log_dir.glob("*.log") if f.stat().st_mtime < cutoff
    )


def _short_id(container: Any) -> str:
    cid = str(container.id) if container.id else ""
    return cid[:12]


def _print_reap_plan(
    orphans: list[Any],
    orphan_snapshots: list[Any],
    stale_logs: list[Path],
    root: Path,
    dry_run: bool,
) -> None:
    if not orphans and not orphan_snapshots and not stale_logs:
        print("nothing to reap. host is clean.")
        return

    if orphans:
        print(f"Containers to reap ({len(orphans)}):")
        for c in orphans:
            print(f"  {_short_id(c)}  {_image_tag(c)}  {_status_disp(c)}  {_task_label(c)}")
    if orphan_snapshots:
        print(f"\nSnapshot images to reap ({len(orphan_snapshots)}):")
        for img in orphan_snapshots:
            tags = (img.tags or ["(untagged)"]) if hasattr(img, "tags") else ["(?)"]
            labels = _image_labels(img)
            sid = labels.get("prehnite.session_id", "(no session label)")
            print(f"  {tags[0]}  session={sid}")
    if stale_logs:
        print(f"\nbatch-logs to delete ({len(stale_logs)}):")
        for f in stale_logs:
            try:
                disp = f.relative_to(root)
            except ValueError:
                disp = f
            print(f"  {disp}")

    if dry_run:
        print("\n(dry-run; nothing changed.)")


def _image_tag(container: Any) -> str:
    try:
        tags = list(container.image.tags) if container.image else []
    except Exception:
        tags = []
    return tags[0] if tags else "(untagged)"


def _status_disp(container: Any) -> str:
    state = str(container.status or "?")
    try:
        ec = container.attrs.get("State", {}).get("ExitCode")
        if ec is not None and state != "running":
            state = f"{state} ({ec})"
    except Exception:
        pass
    return state


def _task_label(container: Any) -> str:
    try:
        labels = dict(container.labels or {})
    except Exception:
        labels = {}
    tid = labels.get("prehnite.task_id")
    return f"task={tid}" if tid else ""


if __name__ == "__main__":
    sys.exit(main())
