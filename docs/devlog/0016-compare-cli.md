# 0016 — `prehnite compare <A> <B>` for cross-snapshot diff

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

`stats` shows one corpus, `batch` runs one corpus, but neither answers
"did my change make things better or worse?". `compare` closes the loop:
diff two snapshots, surface per-task regressions and improvements, and
exit non-zero on any regression so CI can wrap it.

The arc is now: `inspect` (one run) → `stats` (one corpus) → `compare`
(two corpora).

## Inputs accepted

Either positional arg can be:

- A trajectory directory (`trajectories/` or any other dir of
  `*.jsonl` files). Compare walks it and re-uses the same
  `_per_task_rows` / `_overall_metrics` summarizer that `stats`
  uses internally.
- A `.json` file from `prehnite stats --json`. Compare parses the
  `by_task` array and top-level overall fields directly.

Auto-detected by `Path.is_file()` / `Path.is_dir()`. The two args
don't have to be the same kind — a common use case is "save baseline
JSON once, then compare current `trajectories/` against it":

```
prehnite stats trajectories --json > baseline.json
# ... iterate, re-run batches ...
prehnite compare baseline.json trajectories
```

## Per-task categorization

For each task that appears in either snapshot:

| Status | Meaning |
| --- | --- |
| `regression` | task in both, B's `pass_rate < A's` |
| `improvement` | task in both, B's `pass_rate > A's` |
| `unchanged` | task in both, same pass_rate |
| `new` | task only in B |
| `dropped` | task only in A |

Output sorts by status (regressions first), then by `task_id`. The
philosophy: surface the actionable stuff up top, things that need
human attention before things that don't.

## Real output

Self-comparison (sanity):

```
$ prehnite compare baseline.json baseline.json

  task                        A            B    delta  status
  (unknown)              0% (1)       0% (1)      +0%  unchanged
  egress_allowlist     100% (3)     100% (3)      +0%  unchanged
  ...
Overall:
  A: 22/30 (73%)
  B: 22/30 (73%)
  delta: +0%
Tasks: 7 unchanged
```

Synthetic regression case:

```
$ prehnite compare imaginary-baseline.json baseline.json

  task                        A            B    delta  status
  fix_off_by_one       100% (6)      83% (6)     -17%  regression
  hello                100% (7)      71% (7)     -29%  regression
  (unknown)                 N/A       0% (1)        -  new
  egress_allowlist          N/A     100% (3)        -  new
  ...
Overall:
  A: 18/30 (60%)
  B: 22/30 (73%)
  delta: +13%
Tasks: 2 regression, 5 new, 1 dropped
```

Worth noting from the second example: **overall pass-rate went up
(+13%) while two specific tasks regressed.** This is exactly the
scenario eval workflows need surfaced — adding new easy tasks can mask
regressions on existing tasks if you only look at the aggregate. The
per-task table makes it impossible to miss; the exit code (1, because
of the two regressions) makes CI catch it automatically.

## Exit-code policy

- Exit 0: no `regression` rows. Improvements, unchanged, new, and
  dropped are all OK.
- Exit 1: at least one `regression` row.

The asymmetry is deliberate. "Dropped" doesn't fire because removing
a task from the corpus isn't a regression on anything that's still
being measured. "New" doesn't fire because a brand-new task hasn't
had a chance to baseline yet. Only "regression" — a task we have
prior signal on, that got worse — trips the alarm.

## What it doesn't compare (yet)

Pass rate is the only metric compared. The per-task rows in the diff
output also contain runs / median_agent_cmds / median_duration_s /
thoughts_pct (inherited from `stats`), so a JSON consumer can compute
their own deltas. But the table doesn't surface them.

Reason: pass rate is the unambiguous bottom line. A 30% speedup that
comes with a 10% pass-rate drop isn't an improvement; surfacing both
deltas as equally weighted "deltas" muddles the signal. If a real use
case wants latency-regression alerts, that's a separate metric for a
separate alarm.

## What it doesn't do

- **No "significance test" on small samples.** A task with 1 run that
  went from passed to failed shows as `-100%` regression — which is
  technically true but noisy. The `(n)` count next to each rate is
  the only sample-size signal. Statistical sophistication
  (Wilson intervals, etc.) is over-engineering until real corpora
  hit hundreds of runs per task.
- **No --threshold flag** to ignore small deltas. Same reasoning —
  v0 alerts on any drop; users with noisy small-sample corpora can
  filter the JSON output downstream.
- **No baseline-tracking workflow built in.** No "save current as
  baseline.json" command — it's just `stats --json > file`. Keep
  the verbs orthogonal.

## Tests

[`tests/test_cli_compare.py`](../../tests/test_cli_compare.py) — 10
tests via `capsys`, covering:

- **Direction:** regression detected (exit 1), improvement detected
  (exit 0), unchanged
- **Set operations:** new task in B only, dropped task in A only
- **Input modes:** dir-vs-dir, json-vs-json, mixed file-and-dir
- **Output sections:** overall pass-rate summary, task-count summary
- **JSON shape:** asserts the keys (`regressions`, `improvements`,
  `unchanged`, `new`, `dropped`, `overall_delta_pass_rate`,
  `by_task`)
- **Error path:** missing file → exit 2 with stderr message

The fixtures use `TrajectoryWriter` directly for the dir cases (one
file per `_write_run` call), and hand-built dicts for the JSON cases
matching `stats --json` shape.

## Verification

- `uv run pytest` → 113 passed (was 103; +10 new).
- `uv run mypy src/prehnite` → clean.
- Smoke-tested on the real corpus: self-comparison shows zero diff;
  synthetic regression case surfaces two regressed tasks AND five
  new tasks AND one dropped task simultaneously, with exit 1.

## Diff size

```
docs/devlog/0016-compare-cli.md   | ~165 +
src/prehnite/cli.py               |  ~210
tests/test_cli_compare.py         |  ~230 +
```

Pure addition. Reuses `_summarize`, `_per_task_rows`, and
`_overall_metrics` from `stats` — that prior extraction (devlog 0014)
paid off here; no duplication of the summarization logic.
