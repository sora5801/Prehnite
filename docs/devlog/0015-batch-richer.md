# 0015 — `prehnite batch` gains `{tools}`, filters, skip-cache, per-task logs, JSON

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

The `batch` runner that landed in [devlog 0013](0013-batch-runner.md) was
the minimum-viable version: agent template with `{task_id}`, sequential
execution, mtime-based trajectory detection, plain text aggregate. Useful,
but real eval workflows need more:

- **`{tools}` placeholder** so the agent template doesn't have to hard-code
  the long `mcp__prehnite__*` allowed-tools list. The list changes (we've
  added `note` and `describe_task` since v0); pinning it in shell templates
  invites drift.
- **`--filter-tag` / `--filter-difficulty`** so you can run "all bug-fix
  tasks" or "all easy tasks" without invoking the agent on the full corpus
  every time.
- **`--skip-if-passed-within <hours>`** so re-running batch incrementally
  doesn't pay the agent cost for tasks that already passed recently.
  Critical for iterative workflows where you change one task and re-run.
- **`--json` aggregate** for piping into CI annotations, dashboards, or
  diffs across runs.
- **Per-task subprocess log files** so the agent's chatter is preserved
  for debugging without spamming the batch caller's terminal.

## How the runner finds the right trajectory

This is the most subtle piece of the design and worth pinning down.

The agent subprocess writes its trajectory via the MCP server, which lands
at `<root>/trajectories/<task_id>/<utc-stamp>.jsonl`. The batch runner
*doesn't* control that path — the MCP server picks the timestamp at
`start_task` time. So after the agent exits, batch has to figure out which
trajectory file is the one this run produced.

The approach: capture `time.time()` *before* spawning the agent, then look
for the newest `.jsonl` under that task's dir whose `mtime` is `>=` the
recorded start time. This handles four real cases:

1. **Agent ran cleanly and produced one trajectory.** Newest file
   matches. Outcome is read from its `run_finished` event.
2. **Pre-existing trajectories from prior runs.** Their `mtime` predates
   the recorded start; they get filtered out. Test
   `test_batch_ignores_pre_existing_trajectories` locks this in
   (carrying forward from devlog 0013).
3. **Agent timed out partway through.** The MCP server may have written
   a partial trajectory with no `run_finished`. Batch still picks it up
   (so `inspect` can show what the agent managed), but the reported
   status is `timeout`, not whatever the partial trajectory said.
4. **Agent exited without driving the task at all.** No new file
   exists. Status is `no-trajectory`.

Mtime granularity on Windows is usually 100ns and on Linux is filesystem-
dependent (ext4 is nanosecond, but FAT32 is 2-second). For this use case
2-second granularity would be fine — we're comparing against a wall clock
captured seconds ago. No need for a more elaborate scheme.

## Status names changed; spec wins over backward compat

| Old (devlog 0013)      | New (this commit)     |
| ---------------------- | --------------------- |
| `agent_timeout`        | `timeout`             |
| `no_trajectory`        | `no-trajectory`       |
| (didn't exist)         | `skipped`             |

The spec asked for `passed / failed / error / timeout / skipped /
no-trajectory`. Hyphens for multi-word, no `agent_` prefix. Took the spec
literally; tests updated to match. `incomplete` is retained as a
trajectory-derived value (a `run_finished`-less trajectory shouldn't be
silently relabeled — it's a real outcome).

## Skip-if-passed-within details

The skip check runs *before* the subprocess for each task. Three cases:

- `window > 0` AND newest trajectory has `mtime` within the window AND
  its `run_finished.result == "passed"`: status `skipped`, trajectory
  path = the cached one, duration `0.0s`, agent never spawned.
- `window > 0` AND newest is too old: agent runs normally.
- `window > 0` AND newest exists but is `failed`/`error`/`incomplete`:
  agent runs normally. **This is intentional** — a recent failure is
  exactly the case where you *want* a retry, not a skip.

Tests cover all three. The retry-on-failed behaviour is the one most
likely to surprise a future reader (you might expect "skip if recent"
to mean "skip if any recent trajectory exists"); the test name
`test_batch_skip_if_passed_within_runs_on_failed_history` documents the
contract.

For exit-code purposes, `skipped` counts as a pass — by definition the
task already passed recently. So `pass-rate` in the aggregate is
`(passed + skipped) / total`, and exit 0 requires every task to be in
`{passed, skipped}`.

## Subprocess stdout+stderr both go to the per-task log file

Spec says "redirect stderr to `<root>/batch-logs/<task_id>-<UTC-stamp>.log`".
The implementation merges stdout into the same file via
`subprocess.STDOUT`. Reason: most agents (e.g., Claude headless) print
their final RunResult report on *stdout*, not stderr. Throwing that away
would be wasteful; sending it to the terminal would interleave with the
batch's per-task progress lines and make the output unreadable. Merging
into the per-task log keeps each agent's full chatter in its own file,
named so a `ls -t batch-logs/` shows runs in chronological order.

The log dir is gitignored (`/batch-logs/` in `.gitignore`).

## `{tools}` expansion

Module-level constant `_MCP_TOOL_NAMES` lists the seven tools the MCP
server exposes:

```python
_MCP_TOOL_NAMES: tuple[str, ...] = (
    "list_tasks", "describe_task", "start_task",
    "exec", "note", "finish_task", "abort_task",
)
```

`{tools}` expands to `mcp__prehnite__list_tasks,...,mcp__prehnite__abort_task`
— exactly what `claude -p --allowed-tools` expects. Done once before the
task loop since it's constant across tasks.

This list is duplicated knowledge — the MCP server's `build_server()` is
the source of truth. Drift between the two would let an agent be denied
a tool it could call. Acceptable trade-off for v0; a future refactor
could introspect `FastMCP`'s registered tools at import time, but that
pulls in async machinery for a startup-time benefit. Not yet worth it.

## JSON aggregate

Mirrors what the table shows:

```json
{
  "total_tasks": 6,
  "by_status": {"passed": 4, "failed": 1, "skipped": 1},
  "wall_clock_s": 312.5,
  "failed_task_ids": ["fix_log_stats"],
  "tasks": [
    {
      "task_id": "...",
      "status": "passed",
      "duration_s": 12.3,
      "trajectory": "trajectories/..."
    },
    ...
  ]
}
```

Empty corpus emits a stable shape (`total_tasks: 0`, empty arrays) so
downstream tools don't have to special-case it. Same approach as the
`stats --json` empty case from devlog 0014.

## What was deliberately not done

- **No `--task` flag.** The spec only lists `--filter-tag` and
  `--filter-difficulty` for narrowing the task set. Per-task debugging
  can still be done by tagging one task and filtering on the tag, or by
  invoking `prehnite run` headless (which doesn't need batch at all).
- **No parallelism.** Same reasoning as devlog 0013 — the per-session
  host-side egress proxy port management would need real attention
  before running N agents at once. Spec explicitly says "Sequential in
  v1 — parallelism can wait."
- **No retry-on-failure.** Skip-cache lets you re-run incrementally;
  that *is* effectively a retry policy ("retry only failures"). An
  in-batch retry loop would be a different design choice entirely.
- **No per-task budget telemetry beyond wall clock.** Adding e.g. agent
  command count to the aggregate would mean parsing trajectories during
  batch — `prehnite stats` already does that, cleaner separation.

## Tests

[`tests/test_cli_batch.py`](../../tests/test_cli_batch.py) has 16 tests
(was 9). All use the `_patch_subprocess(monkeypatch, fn)` pattern from
devlog 0013 — no real subprocess invocation, no Docker, no network. The
agent impersonator `fn(cmd, kwargs)` returns whatever a real agent would
or raises `subprocess.TimeoutExpired` to test the timeout path.

New tests:

- `test_batch_filter_tag_keeps_only_tagged_tasks`
- `test_batch_filter_difficulty_keeps_only_matching`
- `test_batch_skip_if_passed_within_skips_recent_pass`
- `test_batch_skip_if_passed_within_runs_on_failed_history`
- `test_batch_skip_if_passed_within_runs_on_stale_pass`
- `test_batch_substitutes_tools_placeholder`
- `test_batch_creates_per_task_log_file` (asserts the file is opened
  and `stderr=subprocess.STDOUT` is passed)
- `test_batch_json_aggregate_shape`
- `test_batch_json_empty_dir_has_stable_shape`

The existing tests for status names (`timeout`, `no-trajectory`,
mixed-outcomes) and mtime-based trajectory detection were updated for
the new status strings.

## Verification

- `uv run pytest` → 103 passed (was 95; +9 new, ~6 rewritten).
- `uv run mypy src/prehnite` → clean.
- Manually ran against `tasks/examples`:
  - `--json` produced parseable JSON for both populated and empty cases.
  - `--filter-tag network` narrowed to 2 of 6 tasks (egress_allowlist,
    install_cowsay).
  - `--skip-if-passed-within 24` skipped all 6 tasks (the recent batch
    from earlier in the session is still within the window), exit 0,
    pass-rate 100%.
  - `batch-logs/` populated with one file per task per run.

## Diff size

```
.gitignore                       |    3 +
docs/devlog/0015-batch-richer.md |  ~210 +
src/prehnite/cli.py              |  ~330
tests/test_cli_batch.py          |  ~520
```

No changes outside `cli.py`, tests, and ignored-file metadata.
