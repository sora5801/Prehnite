# 0013 — `prehnite batch <tasks-dir> --agent <cmd>`

**Date:** 2026-05-15
**Status:** ✅ shipped

## Why

The pieces for evaluating an agent against a corpus are now all in
place — `run` for headless smoke runs, `inspect` for single-trajectory
reading, `stats` for corpus aggregation — but nothing actually drives
an agent across every task. The natural workflow ("point my agent at
tasks/examples and tell me how it did") still required a shell loop.
`batch` closes that.

## What it does

```
$ prehnite batch tasks/examples --agent 'claude -p "drive prehnite {task_id}"'

[1/6] egress_allowlist  ... passed         (  12s)  trajectories/egress_allowlist/20260516T07...jsonl
[2/6] fix_log_stats     ... failed         (   8s)  trajectories/fix_log_stats/20260516T07...jsonl
[3/6] fix_off_by_one    ... passed         (  10s)
[4/6] hello             ... agent_timeout  ( 300s)
[5/6] install_cowsay    ... passed         (  45s)
[6/6] merge_configs     ... no_trajectory  (   2s)

Batch summary: 6 tasks
  passed           3
  failed           1
  agent_timeout    1
  no_trajectory    1
  pass-rate:     50%
```

Six outcomes can land in a row:

- **passed / failed / error / incomplete** — read from the run's
  `run_finished` event.
- **agent_timeout** — the agent subprocess didn't return in
  `--per-task-timeout` seconds. The partial trajectory (if any) is
  still surfaced so the user can `prehnite inspect` what the agent
  managed before being killed.
- **no_trajectory** — the agent process returned but no new trajectory
  appeared under `trajectories/<task_id>/`. Likely means the agent
  didn't actually drive the task through MCP. Worth investigating.

Exit code 0 only if every task ended in `passed`; anything else
returns 1 so CI / eval pipelines notice.

## Design choices

### Agent is whatever subprocess you point at

`--agent` is a shell command template. `{task_id}` gets substituted
with the task id (via `str.replace`, not `str.format` — so other braces
in the command aren't a footgun). The agent itself is responsible for
talking to the MCP server. For Claude Code that's the project's
checked-in `.mcp.json`; for a custom agent it's whatever wiring that
agent does. Batch doesn't care.

This means batch is identical for any MCP-driven agent — Claude, GPT
running a local MCP client, a homemade Python agent — they're all
just subprocesses that produce trajectories.

### Sequential, one container at a time

Parallelism is out of scope for v0. Reasons:

- Per-task container resource: 2GB mem, bridged network in `restricted`
  mode means a per-session host-side egress proxy too. Two of those at
  once is doable but the proxy port management and Docker bridge
  contention need a real look.
- Predictable wall time per task is useful for debugging eval drift.
- The eval bottleneck for most users is "is the agent good" not "is my
  laptop fast enough to run six tasks at once".

Adding `--parallel N` is a future call; the structure is sequential
right now and that's deliberate.

### Detecting which trajectory came from this run

The MCP server writes trajectories to
`trajectories/<task_id>/<utc-timestamp>.jsonl`. After the agent exits,
batch looks at that directory and picks the newest file whose mtime is
at or after the moment the agent started. Pre-existing trajectories
from prior runs (which would otherwise hijack the outcome) are
filtered out. There's a dedicated test
(`test_batch_ignores_pre_existing_trajectories`) that locks this in:
an old `passed` trajectory must not leak through into the result of a
no-op agent run.

### Agent runs from `--root` (default cwd)

The agent subprocess inherits `cwd = root`. That keeps relative paths
in MCP server registration (`.mcp.json`) and trajectory output
(`trajectories/`) consistent with how a user normally invokes from the
project root. Pass `--root` if you're driving batch from somewhere
else.

### Trajectory path shown in the live progress line

When a new trajectory landed, the path is appended to the live
progress line. The user can immediately copy it into
`prehnite inspect <path>` to drill into a specific run, without
needing to remember timestamps. For `agent_timeout` we still surface
the partial trajectory if one was written; for `no_trajectory` there's
nothing to show.

## What was deliberately not done

- **No parallelism.** As discussed above.
- **No retry logic.** A failed agent run stays failed in the report.
  The agent's own logic decides whether to retry; batch's job is just
  to call it and record what happened.
- **No agent stdout/stderr capture in the trajectory.** The agent
  subprocess's stdout/stderr is captured by batch but discarded after
  the run — those streams are noise, not signal. The signal is the
  trajectory the agent produced via MCP.
- **No resume.** Re-running batch starts from scratch. A future
  `--resume` could read existing trajectories and skip already-passed
  tasks, but that's a separate axis.

## Tests

[`tests/test_cli_batch.py`](../../tests/test_cli_batch.py) — nine
tests, all via monkeypatched `subprocess.run`. The
`_patch_subprocess(monkeypatch, fn)` helper replaces subprocess.run
with a callable the test scripts directly:

- Pretend to be an agent that writes a `passed` trajectory → batch
  reports passed, exit 0.
- Same but `failed` → batch reports failed, exit 1.
- Raise `TimeoutExpired` → batch reports `agent_timeout`, exit 1.
- Return successfully but write nothing → batch reports
  `no_trajectory`, exit 1.
- Pre-existing old trajectory shouldn't be picked up → batch reports
  `no_trajectory` even though the dir has a `passed` file.
- Three tasks with mixed outcomes (`passed` / `failed` /
  `agent_timeout`) → batch's per-line and summary outputs reflect all
  three, pass-rate 33%, exit 1.
- `--task <id>` filter narrows correctly.
- Missing tasks dir → exit 2.
- Empty tasks dir → exit 0 with a friendly "no tasks" message.

## Verification

- `uv run pytest` → 88 passed (was 79; +9 new).
- `uv run mypy src/prehnite` → clean.
- Manually ran `prehnite batch tasks/examples --agent 'echo no-op
  for {task_id}' --per-task-timeout 5` against the real example
  corpus — every task correctly reported `no_trajectory`, summary
  showed 0% pass rate, exit 1. The plumbing works end-to-end.

## Diff size

```
docs/devlog/0013-batch-runner.md   |  ~165 +
README.md                          |    8 +
src/prehnite/cli.py                |  ~150
tests/test_cli_batch.py            |  ~270 +
```

Pure addition + one CLI subparser. `runner.py`, `sandbox.py`,
`mcp_server.py`, `schemas.py`, etc. untouched.
