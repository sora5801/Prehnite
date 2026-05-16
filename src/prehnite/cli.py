"""Tiny CLI for headless task runs, trajectory inspection, and corpus stats.

Three subcommands:

- `prehnite run <task.yaml>` — runs a task headless (smoke-test driver),
  optionally with a fixed list of agent commands via `--cmd`.
- `prehnite inspect <trajectory.jsonl>` — pretty-prints a captured
  trajectory so you can scan a run without manually parsing JSONL.
- `prehnite stats [<dir>]` — aggregates across every trajectory under a
  directory: per-task pass rate, failure reasons, egress summary.

Use `run` to confirm your Docker image, task YAML, and trajectory wiring
hang together before pointing a real agent at the MCP server. Use
`inspect` to read what an agent (or the headless run) actually did. Use
`stats` to see the shape of a whole corpus of runs.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections import Counter
from pathlib import Path
from typing import get_args

from prehnite.runner import run
from prehnite.schemas import EventType, RunStatus, TrajectoryEvent
from prehnite.tasks.loader import load_task
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
    stats_p.set_defaults(func=_cmd_stats)

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
        print("no trajectories matched.")
        return 0

    _print_stats(runs)
    return 0


class _RunSummary:
    """Everything the stats output needs from one trajectory file."""

    __slots__ = ("path", "task_id", "outcome", "reason", "egress")

    def __init__(
        self,
        path: Path,
        task_id: str | None,
        outcome: str,
        reason: str,
        egress: list[tuple[str, int, bool]],
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.outcome = outcome  # passed | failed | error | incomplete
        self.reason = reason
        self.egress = egress


def _summarize(path: Path, events: list[TrajectoryEvent]) -> _RunSummary:
    task_id: str | None = None
    outcome = "incomplete"
    reason = ""
    egress: list[tuple[str, int, bool]] = []

    for e in events:
        if e.type == "run_started":
            tid = e.data.get("task_id")
            if isinstance(tid, str):
                task_id = tid
        elif e.type == "egress_attempt":
            host = str(e.data.get("host", ""))
            port_raw = e.data.get("port", 0)
            port = int(port_raw) if isinstance(port_raw, (int, float, str)) else 0
            allowed = bool(e.data.get("allowed", False))
            egress.append((host, port, allowed))
        elif e.type == "run_finished":
            outcome = str(e.data.get("result", "incomplete"))
            reason = str(e.data.get("reason", ""))

    return _RunSummary(path, task_id, outcome, reason, egress)


def _print_stats(runs: list[_RunSummary]) -> None:
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
    by_task: dict[str, list[_RunSummary]] = {}
    for r in runs:
        key = r.task_id or "(unknown)"
        by_task.setdefault(key, []).append(r)

    name_w = max(len("task"), max(len(k) for k in by_task))
    print("By task:")
    print(
        f"  {'task':<{name_w}}  {'runs':>4}  {'passed':>6}  {'pass-rate':>9}"
    )
    for tid in sorted(by_task):
        rs = by_task[tid]
        total = len(rs)
        passed = sum(1 for r in rs if r.outcome == "passed")
        rate = f"{(passed * 100) // total}%" if total else "-"
        print(f"  {tid:<{name_w}}  {total:>4}  {passed:>6}  {rate:>9}")
    print()

    # --- failure reasons (only if any) ------------------------------
    fail_reasons = Counter(r.reason for r in runs if r.outcome != "passed" and r.reason)
    if fail_reasons:
        print("Top failure reasons:")
        for reason, count in fail_reasons.most_common(5):
            print(f"  {count:>2}  {_truncate(reason, 100)}")
        print()

    # --- egress summary (only if any) -------------------------------
    all_egress = [att for r in runs for att in r.egress]
    if all_egress:
        runs_with_egress = sum(1 for r in runs if r.egress)
        print(f"Egress ({len(all_egress)} attempts across {runs_with_egress} runs):")
        per_target: dict[tuple[str, int], list[bool]] = {}
        for host, port, allowed in all_egress:
            per_target.setdefault((host, port), []).append(allowed)
        target_w = max(
            len("host:port"),
            max(len(f"{h}:{p}") for h, p in per_target),
        )
        for (host, port), verdicts in sorted(per_target.items()):
            allowed_n = sum(1 for v in verdicts if v)
            denied_n = len(verdicts) - allowed_n
            tag = (
                "allowed"
                if denied_n == 0
                else "denied"
                if allowed_n == 0
                else f"mixed ({allowed_n} ok / {denied_n} denied)"
            )
            target = f"{host}:{port}"
            print(f"  {target:<{target_w}}  {len(verdicts):>3}  {tag}")


if __name__ == "__main__":
    sys.exit(main())
