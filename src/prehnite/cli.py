"""Tiny CLI for headless task runs.

This is a smoke-test driver, not an agent. It runs a task with an empty agent
command list — meaning setup and verify alone determine the outcome — or with a
fixed list of commands passed via `--cmd`.

Use it to confirm your Docker image, task YAML, and trajectory wiring all hang
together before pointing a real agent at the MCP server.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prehnite.runner import run
from prehnite.schemas import RunStatus
from prehnite.tasks.loader import load_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prehnite")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Run a task headless and print the result")
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

    args = parser.parse_args(argv)

    task = load_task(args.task)
    result = run(task, root=args.root, agent_commands=args.cmd or None)

    print(f"task:        {result.task_id}")
    print(f"status:      {result.status.value}")
    print(f"reason:      {result.reason}")
    print(f"trajectory:  {result.trajectory_path}")
    print(f"container:   {result.container_id}")

    return 0 if result.status is RunStatus.PASSED else 1


if __name__ == "__main__":
    sys.exit(main())
