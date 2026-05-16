# 0010 — `prehnite inspect` CLI for reading trajectories

**Date:** 2026-05-15
**Status:** ✅ shipped

## Why

Up to now, reading a trajectory meant either `cat traj.jsonl` (raw JSONL,
unreadable) or a one-off Python pretty-printer pasted into a shell. The
trajectory file is supposed to be the artifact a developer reaches for to
understand a run — that should be one command, not a snippet.

`prehnite inspect <trajectory>` makes the trajectory a first-class
read-target.

## What it does

```
$ uv run prehnite inspect trajectories/egress_allowlist/20260516T061720Z.jsonl

[  0] 06:17:21 run_started      task=egress_allowlist  image=prehnite-base:latest  network=restricted
[  1] 06:17:21 setup_command    (157ms)  rm -rf /workspace/*  -> exit 0
[  2] 06:17:28 agent_thought
        | Plan: 1. curl https://example.com with -o /dev/null -w "%{http_code}"
        | to capture only the HTTP status code...
[  3] 06:17:31 egress_attempt   ALLOWED  example.com:443  (32ms)  matched allowlist
[  4] 06:17:31 agent_command    (191ms)  curl -sS -o /dev/null ...  -> exit 0
        stdout: exit: 0\n
[  7] 06:17:51 egress_attempt   DENIED   www.iana.org:443  (0ms)   not in allowlist
...
  passed: all verify checks passed
  4 agent_command, 3 agent_thought, 2 egress_attempt, 1 run_finished, 1 run_started, 1 setup_command, 4 verify_command
```

One line per command-like event (cmd / exit code / timing on the same
line, `stdout` / `stderr` indented under it if non-empty). Thoughts wrap
at 78 columns with a `|` gutter. Egress events use `ALLOWED`/`DENIED`
markers and surface the deny reason. Final summary at the bottom shows
the run's outcome plus per-type counts in alphabetical order.

## Flags

- `--full` — disable truncation. Commands and output are shown verbatim,
  thoughts respect their original line breaks.
- `--type <type>` (repeatable, with `choices=` from `EventType`) — show
  only matching events. Useful for `--type agent_thought` (skim reasoning)
  or `--type egress_attempt` (audit the network).
- `--summary-only` — skip events, print only the summary. Useful for
  `for f in trajectories/**/*.jsonl; do echo "$f"; prehnite inspect "$f"
  --summary-only; done` to get a one-liner per run.
- `--no-summary` — drop the summary, keep the events. Useful for piping
  to `wc -l` or grep.

## Implementation notes

- Lives in [`src/prehnite/cli.py`](../../src/prehnite/cli.py) as a second
  subparser alongside `run`. Dispatch is `set_defaults(func=...)` per
  subparser so the main function is just `args.func(args)`.
- The event-type choices come from `typing.get_args(EventType)` — adding
  a new event type to the `Literal` automatically updates the `--type`
  choices. One source of truth.
- ASCII-only output (`->` not `→`, `,` not `·`). Windows `cmd.exe` and
  PowerShell with non-UTF-8 code pages render this without issues. Any
  non-ASCII content in trajectory data (e.g. em-dashes in agent
  thoughts) is passed through verbatim; if the user's console can't
  render it, that's a console-config issue (set `PYTHONUTF8=1` or
  `chcp 65001`), not something to mangle in the formatter.
- Truncation uses 80 cols for cmd, 200 chars for stdout/stderr. The
  three-dot ellipsis (`...`) replaces the tail, not the head, because
  command prefixes are usually more identifying than suffixes.

## Tests

[`tests/test_cli_inspect.py`](../../tests/test_cli_inspect.py) — seven
tests, all in-process via `pytest`'s `capsys`. No subprocess invocation
or shell quoting. The shared `_write_sample_trajectory` fixture covers
every `EventType` so the format-renderer-by-type code path is exercised
in one test, and the other tests focus on flag behavior:

- Every event type renders (presence assertions)
- Summary includes per-type counts
- `--type agent_thought` filters out everything else
- `--summary-only` suppresses events but keeps the summary
- `--full` shows untruncated commands
- Missing file exits 2 with an error on stderr
- Incomplete trajectory (no `run_finished`) gets `(incomplete - …)`

## Verification

- `uv run pytest` → 63 passed (was 56; +7 new).
- `uv run mypy src/prehnite` → clean.
- Manually rendered every example trajectory in the repo — formats
  look right across the failed/passed/incomplete cases.

## Diff size

```
docs/devlog/0010-inspect-cli.md  | ~95 +
README.md                        |   8 +-
src/prehnite/cli.py              |  ~150 (was 58)
tests/test_cli_inspect.py        | ~180 +
```

Pure addition + one CLI refactor (subcommand dispatch via
`set_defaults(func=)`). No production code outside `cli.py` touched.
