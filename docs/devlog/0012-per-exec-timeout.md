# 0012 — Per-exec timeout in `Sandbox.exec`

**Date:** 2026-05-15
**Status:** ✅ shipped

## Why

[Devlog 0001](0001-mcp-wired-and-driven.md) flagged this as one of the
brittle things in v0:

> **No timeout on `exec`.** The task spec has `timeout_seconds: 120` but
> the runner doesn't enforce it on individual `exec` calls — a wedged
> command in the agent phase will hang the session until the agent
> gives up or the MCP client disconnects.

A real eval workflow can't tolerate a single bad agent command pinning
a session forever. Closing that gap now that the rest of the spine has
stabilised.

## What changed

Two-line schema addition + one-line cmd wrapping in the sandbox:

- `Task` gains `exec_timeout_seconds: int = Field(default=60, gt=0, le=600)`.
  Default of 60s is generous for typical commands (pip install of a
  small package is sub-10s; a heredoc write is sub-second) but tight
  enough that a runaway gets caught fast. Upper bound 600s keeps
  someone from accidentally setting it to "an hour" and re-creating
  the original problem.
- `Sandbox.exec(cmd)` now constructs the docker cmd as
  `["timeout", str(t), "sh", "-c", cmd]` instead of just
  `["sh", "-c", cmd]`. GNU `timeout` returns exit code 124 when the
  wall-clock budget runs out, which propagates through to
  `CommandResult.exit_code` — a clear, conventional "killed by
  timeout" signal that callers can distinguish from real non-zero
  exits.
- An optional `timeout_seconds: float | None = None` kwarg on
  `Sandbox.exec` lets a single call override the task default. The
  formatter accepts fractional seconds (`{:g}` formats `0.5` as
  `"0.5"`), useful in tests and for any future caller doing
  fast-failing probes.

## Why GNU `timeout` and not threading

Three approaches were on the table:

| Approach | Verdict |
| --- | --- |
| **Wrap cmd with GNU `timeout`** | ✅ Picked. Single-line change. Exit code 124 is the standard convention. Works inside the sandbox without host coordination. |
| Thread + cancel docker exec_run | Docker SDK doesn't cleanly cancel mid-stream. Would need socket-level abort and recovery from a partial exec. ~50 lines of fragile code. |
| Set socket-level timeout on the daemon connection | Times out the API call but leaves the in-container process running orphaned. The wedged process keeps holding container resources until container.stop() takes it down. |

GNU `timeout` ships with coreutils (already in `prehnite-base` via the
Dockerfile's base layer). It kills the *process group* on timeout, so
shell pipelines and child processes get cleaned up — not just the
leaf command. SIGTERM first with a 10s grace, then SIGKILL.

## The trajectory still shows the user-readable cmd

`CommandResult.cmd` continues to record the original `cmd` arg passed
to `Sandbox.exec`, not the `timeout`-wrapped form. A trajectory reader
sees `cmd: "echo hello"`, not `cmd: ["timeout", "60", "sh", "-c",
"echo hello"]`. Wrapping is a runtime implementation detail; the
trajectory documents what the *agent* asked for. The
`test_exec_records_cmd_as_user_wrote_it_not_the_wrapped_form` test
locks this in so a future refactor can't silently change it.

A timeout-killed exec still shows up in `inspect` output:

```
[ 12] 03:14:15 agent_command   (60012ms)  while true; do :; done  -> exit 124
```

Duration ≈ the budget; exit code 124. A reader can tell at a glance.

## What's deliberately not done

- **No per-call timeout for setup/verify commands.** Those go through
  the same `Sandbox.exec` so they pick up the same default. The
  Runner / MCP server don't override — if a setup step legitimately
  needs longer (e.g. an `apt install`), the right knob is bumping
  `exec_timeout_seconds` on the task. If we ever need fine-grained
  per-phase timeouts, that's a separate change.
- **No total-run budget enforcement.** `Task.timeout_seconds` (the
  pre-existing 120s default) is still parsed but still unused. That
  field is reserved for "wall budget for the entire run" if/when we
  add that enforcer. The per-exec timeout addresses the immediate
  problem (wedged command); the total budget is a separate axis
  about multi-hour sessions.
- **No automatic retry on timeout.** Exit 124 just propagates. The
  agent sees it and can decide what to do (retry, give up, narrow
  the command). That matches the philosophy elsewhere — surface the
  signal, don't paper over it.

## Tests

- [`tests/test_schemas.py`](../../tests/test_schemas.py): +3 tests
  — default value of 60, lower bound rejects 0, upper bound rejects
  601.
- [`tests/test_sandbox_exec.py`](../../tests/test_sandbox_exec.py):
  +4 tests via the fake container — verifies the cmd Docker actually
  receives is the wrapped form with the task default, that the kwarg
  override works, that fractional seconds format cleanly, and that
  `CommandResult.cmd` keeps the unwrapped user cmd.
- [`tests/test_sandbox.py`](../../tests/test_sandbox.py): +1
  integration test (Docker required) — `sb.exec("sleep 5")` against
  a task with `exec_timeout_seconds=1` exits 124 in well under 30s
  wall time. Suite total wall time went from 18s to 22s, which is
  the ~1s sleep + ~3s container start/stop overhead.

## Verification

- `uv run pytest` → 79 passed (was 72; +3 schema, +4 unit, +1
  integration, plus an existing fake test now passes the wrapped cmd
  through without complaint).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
docs/devlog/0012-per-exec-timeout.md | ~100 +
src/prehnite/sandbox.py              |  ~18 +
src/prehnite/schemas.py              |    1 +
tests/test_sandbox_exec.py           |  ~30 +
tests/test_sandbox.py                |  ~17 +
tests/test_schemas.py                |  ~10 +
```

One open punchlist item from v0 closed. The other two remaining from
devlog 0001 — single-process session state, container leaks on hard
kill — are still open and unchanged.
