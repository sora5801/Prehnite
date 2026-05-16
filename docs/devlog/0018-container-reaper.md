# 0018 — `prehnite reap` for host cleanup

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

Closes the last brittleness item from [devlog 0001](0001-mcp-wired-and-driven.md):

> **Container leaks on hard kill.** `Sandbox.stop()` runs in a `finally`,
> but if the MCP process is `SIGKILL`'d the container outlives it. We
> rely on Docker's own `auto_remove` being **off** (a deliberate choice
> so failures are inspectable), which means an orphan container per
> crashed run. A reaper script keyed on a label set at create time
> would close this loop.

Done. The label-based reaper described there is now real:
`Sandbox.start()` sets `labels={"prehnite": "true", "prehnite.task_id":
<id>}` on every container; `prehnite reap` finds them and removes the
ones that aren't part of a live MCP session.

Two kinds of host detritus get cleaned up:

- **Orphan containers** — `prehnite-*` containers whose `container_id`
  isn't in any `<root>/sessions/*.json` descriptor. Includes the
  containers left behind by the now-defunct restricted-mode sessions
  that the rehydrate step (devlog 0017) drops.
- **Stale `batch-logs/` files** — `*.log` files older than
  `--older-than-hours` (default 24).

## How it identifies prehnite containers

Two complementary filters, OR'd and de-duped:

1. **`label=prehnite=true`** — every container `Sandbox.start()` creates
   from now on. Cleanest match, but pre-this-commit containers won't
   have the label.
2. **`ancestor=prehnite-base`** — image-name filter. Catches the
   pre-label-era orphans that the user might still want swept up. Has
   a false-positive risk if the user runs unrelated containers from
   `prehnite-base`, but that's unusual and `--dry-run` lets the user
   verify.

The cross-reference against live sessions reads every JSON in
`<root>/sessions/`, extracts `container_id`, and refuses to reap
anything in that set. So an MCP server with active sessions can have
the reaper run alongside it without ripping any active workspace out
from under an agent.

## Real output

```
$ prehnite reap --dry-run --older-than-hours 1

batch-logs to delete (7):
  batch-logs\egress_allowlist-20260516T010938Z.log
  ...
  batch-logs\merge_configs-20260516T010939Z.log

(dry-run; nothing changed.)
```

Currently the host has zero orphan containers — every recent test run's
container was cleanly removed in `Sandbox.stop()`. The 7 stale logs are
from a `batch` invocation earlier today.

With actual orphans the output looks like:

```
Containers to reap (3):
  4dfa6969a519  prehnite-base:latest  exited (137)  task=fix_off_by_one
  058182da28cf  prehnite-base:latest  exited (0)    task=hello
  ...

Reaped 3 containers and 7 batch logs.
```

Short id + image tag + exit status + task hint per row. The task hint
comes from the `prehnite.task_id` label, so a human poking around with
`docker ps` can see which task each container came from too.

## Why default is "reap" not "dry-run"

`docker rm` doesn't ask for confirmation. `git rm` doesn't either. The
common case is "I know I want to clean up." Users who want a preview
add `--dry-run`. The output explicitly says "Reaped N containers and M
batch logs" so there's no surprise about what happened.

## Incidental fix: mtime precision on Windows

While running the new tests, the suite started flaking on
`test_batch_records_passed_outcome_from_trajectory` and
`test_batch_multiple_tasks_mixed_outcomes` — but only when run as part
of the full suite, not in isolation. Investigation:

```
started_at = 1778928495.3274703   (time.time())
file mtime = 1778928495.3269048   (stat().st_mtime after write)
diff:        -0.0005655
```

Windows reports file mtime with lower precision than `time.time()`. A
file written 0.5ms *after* `time.time()` can stat as having an mtime
~0.5ms *before* it. `_latest_new_trajectory`'s `mtime >= since_wallclock`
check filtered the trajectory out, batch reported `no-trajectory`
instead of `passed`/`failed`, tests failed.

This was a pre-existing bug from [devlog 0013](0013-batch-runner.md);
it just happened to pass under prior test ordering / timing.

Fix: 1 second of slop on the cutoff:

```python
cutoff = since_wallclock - 1.0
new_files = [f for f in ... if f.stat().st_mtime >= cutoff]
```

1s is generous but safe — `test_batch_ignores_pre_existing_trajectories`
sets old trajectories to mtime=1_000_000.0 (year 1970), way beyond the
slop. In production, a "trajectory file written within 1s before batch
started" is rare enough to ignore, and would belong to a different
task_id anyway (so wouldn't be picked up).

## What the reaper deliberately doesn't do

- **No deletion of session JSON files.** Those are cleaned up by the
  MCP server's rehydrate path on next startup (devlog 0017). A future
  reaper version could do it, but keeping reap container-focused
  avoids stepping on the rehydrate logic.
- **No deletion of trajectory files.** Trajectories are the *output*
  of runs — they're what `stats`/`compare` consume. Different cleanup
  policy (probably never, or based on disk-pressure / age). Out of
  scope.
- **No `--force` to reap live sessions.** Too easy to footgun. If a
  user genuinely wants to nuke a live session, they can `docker rm -f`
  by hand.
- **No filtering by exit status.** Could report only failed-exit
  containers (`exit_code != 0`) but that adds a flag for marginal
  benefit. If you don't want a particular container reaped, it
  shouldn't be in the candidate set in the first place.

## Tests

[`tests/test_cli_reap.py`](../../tests/test_cli_reap.py) — 10 tests via
a fake `_FakeDockerClient` + `_FakeContainer` (`.remove(force=True)`
records the call so tests can assert it was reaped). No real Docker
daemon contacted. Coverage:

- Orphan with `prehnite=true` label → reaped
- Container with id in a session JSON → kept
- Mixed live+orphan → only orphan reaped
- Pre-label container with `image=prehnite-base:*` → reaped (legacy sweep)
- Unrelated container (different image, no label) → kept
- `--dry-run` doesn't remove anything
- Stale batch-logs (>24h) → deleted; fresh logs kept
- `--dry-run` keeps stale logs
- No orphans + no logs → friendly "nothing to reap" message
- Missing `sessions/` dir → reaper still works
- Docker daemon unreachable → exit 2 with stderr error

## Verification

- `uv run pytest` → 134 passed (was 123; +10 reaper + 1 mtime-fix
  carryover that was previously flaky).
- `uv run mypy src/prehnite` → clean.
- `prehnite reap --dry-run` against real Docker: 0 orphan containers
  (clean state), 7 stale batch-logs flagged, no errors.

## Diff size

```
docs/devlog/0018-container-reaper.md | ~190 +
README.md                            |    6 +
src/prehnite/cli.py                  | ~210 + (reap subcommand) + ~10 (mtime slop fix)
src/prehnite/sandbox.py              |    6 + (labels kwarg)
tests/test_cli_reap.py               | ~290 +
```

The punchlist items from devlog 0001 are now all closed: per-exec
timeout (devlog 0012), single-process session state (devlog 0017),
container leaks on hard kill (this one).
