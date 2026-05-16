# 0011 — `prehnite stats` for corpus-level trajectory aggregation

**Date:** 2026-05-15
**Status:** ✅ shipped

## Why

`inspect` (devlog 0010) made one trajectory readable. `stats` makes
the *corpus* readable. The "evaluate or train a coding agent" use
case from CLAUDE.md needs per-task pass rates, not just individual
runs — that's the basic eval signal. Failure-reason aggregation
catches drift in run shape (a sudden spike in "no agent activity"
means agents are giving up, not failing to fix). Egress aggregation
surfaces what restricted-mode runs are actually talking to.

## What it does

```
$ prehnite stats trajectories

24 trajectories across 6 tasks

By outcome:
  passed        16
  failed         7
  error          1

By task:
  task              runs  passed  pass-rate
  (unknown)            1       0         0%
  egress_allowlist     2       2       100%
  fix_log_stats        4       2        50%
  fix_off_by_one       5       4        80%
  hello                6       4        66%
  install_cowsay       4       3        75%
  merge_configs        2       1        50%

Top failure reasons:
   4  no agent activity (verify ran on untouched workspace)
   1  verify failed: ['cd /workspace && test "$(python3 stats.py)" = "3"']
   1  sandbox start: image 'prehnite-base:latest' not found locally - build it first ...

Egress (6 attempts across 3 runs):
  example.com:443               2  allowed
  files.pythonhosted.org:443    1  allowed
  pypi.org:443                  1  allowed
  www.iana.org:443              2  denied
```

That output came from running stats over the actual `trajectories/`
folder accumulated across this session's commits. Real signal:

- The "no agent activity" reason from devlog 0002 is the dominant
  failure mode (4/7). That's evidence the distinction *was* worth
  carving out — anyone training on this corpus can now filter
  "gave up" trajectories out before computing learning signal,
  using exactly the `reason` substring.
- An `(unknown)` row for one early trajectory missing a `run_started`
  event. Graceful handling rather than a crash.
- Per-task pass rates range 50–100%, with `fix_log_stats` (the
  intentionally-multi-shot task) sitting at 50% — agents only catch
  both bugs about half the time, which is the intent.
- Egress section shows the deny path for `www.iana.org:443` is
  exercised twice (the two `egress_allowlist` runs) and never
  accidentally allowed.

## Design choices

- **Lives in `cli.py` alongside `run` and `inspect`.** Same dispatch
  pattern (`set_defaults(func=_cmd_stats)`). No new module — the
  formatter is ~120 lines and only the CLI uses it.
- **`_RunSummary` dataclass-lite.** A `__slots__` class instead of
  `@dataclass` to avoid pulling pydantic into the hot path. Each
  trajectory file gets reduced to `(path, task_id, outcome, reason,
  egress_list)` and the printer works from those — keeps the loaded
  `TrajectoryEvent` objects from staying in memory longer than the
  one pass that needs them.
- **Outcome `incomplete` is a synthetic value.** It's not in
  `RunStatus` — it represents "no `run_finished` event found" (the
  process died, or the run was interrupted). Stats surfaces it so
  you can see how many runs hit that path.
- **Egress section omitted entirely when no `egress_attempt` events
  exist.** Otherwise every corpus that doesn't use restricted mode
  would have a noisy "0 attempts" line.
- **Failure reasons grouped by exact-string-match.** That makes
  "no agent activity" cluster correctly (it's a fixed string) but
  fragments `verify failed: [...]` outputs because each failed
  verify command's list is in the reason. Acceptable for v0 — the
  signal is mostly carried by the "no agent activity" cluster, and
  a future stats refinement could normalize the `verify failed:`
  prefix separately.
- **`--task` filter applies after summarization, not before.** The
  per-task table is still rendered (just with one row) so the output
  shape stays consistent. Useful when piping through one task at a
  time in a loop.

## Tests

[`tests/test_cli_stats.py`](../../tests/test_cli_stats.py) — nine
in-process tests via `capsys`. The `_build_corpus(tmp_path)` helper
writes five trajectories spread across three tasks with mixed
outcomes and one restricted-mode run, then individual tests assert
each piece of the output:

- total + outcome counts
- per-task pass rate computed correctly
- top failure reasons cluster identical strings
- egress summary shows host:port + verdict
- egress section is omitted when no attempts
- `--task` filter narrows correctly
- incomplete runs counted
- missing dir exits 2 with stderr error
- empty dir prints a friendly message and exits 0

## Verification

- `uv run pytest` → 72 passed (was 63; +9 new).
- `uv run mypy src/prehnite` → clean.
- Manually ran against the actual `trajectories/` folder (24 files);
  output matched expectations and surfaced the `(unknown)` early run.

## Diff size

```
docs/devlog/0011-stats-cli.md   | ~110 +
README.md                       |   5 +
src/prehnite/cli.py             |  ~140
tests/test_cli_stats.py         | ~175 +
```

Pure addition. No changes to runner, sandbox, schemas, mcp_server,
trajectory writer, or example tasks.
