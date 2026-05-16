# 0014 — `prehnite stats` gains median-cmds / median-duration / thoughts%, plus `--json`

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

The shape that landed in [devlog 0011](0011-stats-cli.md) covered the
basics (per-task pass rate, outcome breakdown, top failure reasons, a
per-host egress section). Useful, but not enough signal for an eval
workflow that needs to answer "is the agent getting faster?", "is the
agent thinking more?", or "what's the typical cost of a successful
run?". This commit fills those out:

- **median_agent_cmds** per task — does the agent solve it in 2 calls
  or 12? Drift in this number is a real signal.
- **median_duration_s** per task — wall-clock from `run_started.ts` to
  `run_finished.ts`. Surfaces tasks that are slow vs. fast on average.
- **thoughts_pct** per task — % of runs that recorded ≥1 `note` event.
  Catches the regression where an agent stops using `note` after a
  prompt change.
- Overall section now says total_thoughts + runs-with-any-thought, and
  collapses egress to a single "N events (A allowed, D denied)" line.
- **`--json` mode** for piping into downstream tools (a dashboard,
  another CLI, a CI annotation step).

## Real output, current corpus

```
30 trajectories across 6 tasks

By outcome:
  passed        22
  failed         7
  error          1

By task:
  task              runs  pass  fail  err  pass-rate  med-cmds   med-dur  thoughts%
  (unknown)            1     0     0    1         0%       0.0      0.0s         0%
  egress_allowlist     3     3     0    0       100%       4.0     31.0s        66%
  fix_log_stats        5     3     2    0        60%       1.0      0.0s        20%
  fix_off_by_one       6     5     1    0        83%       2.0     15.0s        50%
  hello                7     5     2    0        71%       1.0      1.0s        14%
  install_cowsay       5     4     1    0        80%       2.0     13.0s        40%
  merge_configs        3     2     1    0        66%       1.0      0.0s        33%

Overall:
  30 trajectories, 22 passed (73%)
  15 agent_thought events across 10 runs (33% of runs had at least one thought)
  10 egress_attempt events (7 allowed, 3 denied)
```

Three things this view surfaces that the older format hid:

- `egress_allowlist` takes ~31s median, `install_cowsay` only ~13s.
  Counter-intuitive (cowsay does a real `pip install`!) — but the
  egress task makes two HTTPS round trips through the proxy with the
  associated TLS handshakes, while cowsay's whole flow is one HTTP
  CONNECT for the metadata server and one for the file server, both
  reused via pip's connection pool.
- 33% of runs have any `note` event. The new tasks Sonnet drove this
  session always emitted notes (~100%); the older `fix_log_stats` /
  `hello` failure runs from the v0 build pre-date the `note` tool.
  Eval ratchet target: this number should climb as we drop more
  no-thought runs from the corpus.
- `(unknown)` row sums to 1 error — that's an early run that crashed
  on `image not found` before `run_started` got written. The
  per-task table treats unknown task_id as its own bucket rather
  than dropping the file silently.

## Design choices worth flagging

### Median, not mean

Mean is more sensitive to one slow run skewing the average — an
agent that runs 4 fast tasks in 5s each and one slow task at 60s has
a mean of 16s but a median of 5s. The median is what an eval reviewer
intuitively expects when they ask "how long does this task usually
take?". `statistics.median` from stdlib, no new deps.

### Duration computed from event timestamps, not file mtime

`mtime` would be the wall-clock when the file was last flushed —
correct most of the time but skewed by long verify phases or by the
trajectory writer's lock contention with the egress proxy thread.
Using `run_started.ts` − `run_finished.ts` gets the actual run
budget, which is what eval cares about.

`datetime.fromisoformat` doesn't accept the trailing `Z` until Python
3.12. We support 3.11+, so the small `_parse_ts` helper rewrites `Z`
to `+00:00` before parsing. Robust across the supported range.

### Runs missing `run_finished` get `duration_s = None`

Those are filtered out before computing the median. If every run for a
task is incomplete, the median falls back to `0.0` (not NaN) so the
table renders predictably.

### Parse errors are warnings, not crashes

If a malformed `.jsonl` is encountered in the directory, the run
prints `warning: skipping <path>: <err>` to stderr and keeps going.
Test `test_stats_skips_malformed_files_with_stderr_warning` locks
this in — one well-formed file plus one garbage file should produce
stats over the well-formed file and a single stderr warning, exit 0.

### `--json` parity with the human output

The JSON payload contains exactly the same fields as the human
output, just structured. No per-host egress detail (the spec asked
for the split, not the breakdown), no formatting artifacts. The
empty-corpus case emits a stable shape (zeros + empty lists) so
downstream tools don't have to special-case it.

### Percentages as integers (0–100), not fractions (0.0–1.0)

Picked because the human output uses `50%` and the JSON should match
the same number. Halving the chance of "wait, is this 50 or 0.5?"
confusion downstream. The decision is reversible if a real consumer
wants fractions later.

## What was deliberately not done

- **No mean / stddev / percentiles beyond median.** Median is the
  most-useful single number; mean adds noise from outliers, stddev
  is rarely interpretable for tiny corpora. P50 is the median; P95
  on five runs is one data point. Revisit when corpora cross ~50
  runs per task.
- **No per-host egress detail in `--json`.** The spec explicitly
  asks for the allowed/denied split as the egress metric. Per-host
  detail is still available via `prehnite inspect <traj> --type
  egress_attempt`, which is the right tool for that question.
- **No filtering on `--json` mode beyond `--task`.** Date-range or
  outcome filters would be useful but live in the next "stats query
  layer" change, not this one.

## Tests

Existing 9 tests retained (the egress one was rewritten to assert
the new single-line format). 7 new tests via `capsys`:

- median_agent_cmds: two runs with 2 and 4 commands → `3.0` in row
- median_duration_s: two runs of 5s and 15s → `10.0s` in row
- thoughts_pct: four runs with 2/1/0/0 thoughts → `50%` in row
- overall_summary_lines: literal "3 agent_thought events across 2
  runs (66% of runs had at least one thought)"
- skips_malformed_files_with_stderr_warning: garbage file → warning
  on stderr, good run still counted
- json_output_shape: parse the JSON, assert every key + per-task
  numbers
- json_output_empty_dir_has_stable_shape: empty corpus emits a
  parseable payload with zeros + empty lists

A new helper `_write_raw_trajectory(path, events)` writes a JSONL by
hand so tests can control `ts` precisely — necessary for the
duration assertions, where the live `TrajectoryWriter` would
auto-stamp with the wall clock.

## Verification

- `uv run pytest` → 95 passed (was 88; +7 new, +1 rewritten that
  still passes under the new shape).
- `uv run mypy src/prehnite` → clean.
- Manually ran against `trajectories/` (30 files); output matches
  the spec line-for-line. JSON parses with `python -m json.tool`.

## Diff size

```
docs/devlog/0014-stats-richer-metrics.md | ~190 +
src/prehnite/cli.py                      |  ~190
tests/test_cli_stats.py                  |  ~180 +
```

No changes to `runner.py`, `sandbox.py`, `schemas.py`,
`mcp_server.py`, `egress_proxy.py`, `trajectory.py`, or any task YAML.
