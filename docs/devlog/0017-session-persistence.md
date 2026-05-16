# 0017 — MCP session persistence (with one caveat)

**Date:** 2026-05-16
**Status:** ✅ shipped, with a deliberate restricted-mode limitation

## Why

Closes one of the brittleness items from [devlog 0001](0001-mcp-wired-and-driven.md):

> **Single-process session state.** All session dicts live in the
> `mcp_server.build_server()` closure. If the MCP server crashes mid-run,
> the trajectory file is sealed at whatever the last write was, but
> there's no way to resume.

Before this commit: kill the MCP server mid-run, agent's `session_id`
becomes meaningless on the next process start, the docker container
keeps running orphaned, and the trajectory has no `run_finished` event.
A long batch eval would lose the session it was 90% through.

After: kill the MCP server mid-run, restart, agent's `session_id` still
resolves. The container is reattached (it was detached + `sleep
infinity`, so it survived), the trajectory writer reopens in append
mode with the seq counter recovered, the agent_command_count is
recounted from the trajectory, and `exec` / `note` / `finish_task` work
exactly as if nothing happened.

## How

Per-session JSON descriptor at `<root>/sessions/<session_id>.json`,
written on `start_task` and deleted on `finish_task` / `abort_task`:

```json
{
  "session_id": "abc123",
  "task": { /* Task.model_dump() */ },
  "container_id": "deadbeef...",
  "trajectory_path": "trajectories/demo/...jsonl",
  "started_at_iso": "2026-05-16T...",
  "network_mode": "none"
}
```

Three pieces working together:

1. **`Sandbox.attach(container_id)`** — new method that calls
   `docker.containers.get(container_id)` instead of `containers.create(...)`.
   Re-binds an existing Sandbox to a still-running container. ~10 lines.
2. **`TrajectoryWriter.open()` recovers the seq counter** — when the
   file already exists with content, the writer parses the last line's
   `seq` value and continues from there. So `start_task` → write 0..2,
   server restart, `exec` → write 3 (not 4 again at 0). The line-count
   recovery is O(file size) but only one JSON parse (last non-empty
   line); trajectories are kilobytes, so this is microseconds.
3. **`_rehydrate_sessions(root)` runs at server startup** — scans
   `sessions/*.json`, for each: drops restricted-mode descriptors,
   tries to attach the Sandbox (drops if the container is gone),
   reopens the writer, counts `agent_command` events from the
   trajectory, populates the sessions dict.

`build_server(sessions=None)` calls `_rehydrate_sessions(_root())`. Tests
that need a clean slate still pass `sessions={}` explicitly — no
behaviour change for the existing test surface.

## What `agent_command_count` is NOT in the descriptor

The session JSON does *not* persist `agent_command_count`. Tempting
("just write it on every exec!") but wrong:

- Persisting it on every `exec` means an extra fsync per call, doubles
  the I/O for a single agent action.
- More importantly: the trajectory file is the source of truth for
  what happened. If the descriptor's counter ever diverges from the
  trajectory's `agent_command` events, the descriptor is wrong, full
  stop. So we just recount from the trajectory at rehydrate time and
  the counter can never drift.

`_count_agent_commands(trajectory_path)` is a small helper that reads
the file via `read_trajectory()` and returns the count.

## The deliberate caveat: restricted mode can't resume

The egress proxy listens on an OS-assigned port baked into the
container's `HTTP_PROXY` env var at create time. After an MCP server
restart:

- A new proxy could try to re-bind that exact port, but it's racy —
  another process may have grabbed it in the interim, and the bind
  fails with "address already in use".
- We can't change the container's env vars after creation (Docker
  doesn't allow it), so we can't redirect to a new port.

For v0, `_rehydrate_sessions` checks `network_mode == "restricted"` and
drops those sessions with a `stderr` warning. The container keeps
running (the reaper from a future devlog will clean those up) and the
agent's next call sees "unknown session_id" — the cleanest signal we
can give without lying.

`none` and `full` mode sessions resume cleanly (no proxy to coordinate).
For the corpus we currently ship, that's 5 of 6 example tasks; only
`install_cowsay` and `egress_allowlist` use restricted.

**Future fix:** deterministic per-session port, allocated up front (e.g.
hash session_id into a 49152-65535 port range, retry on collision), and
persist the port number in the session descriptor so the new process
can re-bind exactly that. Defer until someone actually hits the
restricted-mode resume case in anger.

## What lives in `<root>/sessions/`

```
sessions/
├── abc123def456.json     # live session, not yet finish_task'd
├── 789a...json           # ditto
```

The dir grows during active sessions and shrinks back to empty as they
complete. Two failure modes for the descriptors:

- `finish_task` / `abort_task` raise before reaching the cleanup line:
  the descriptor stays, but the in-memory session is gone. On next
  startup, `_rehydrate_sessions` notices the container is stopped (the
  previous process did stop it before the exception) and drops the
  descriptor with a "container is gone" warning. Self-healing.
- MCP server `SIGKILL`'d mid-run: descriptor + container both survive.
  Next startup rehydrates cleanly.

`/sessions/` is gitignored — it's runtime state, not configuration.

## Tests

[`tests/test_trajectory.py`](../../tests/test_trajectory.py) — 2 new
tests for the writer's seq recovery on reopen of an existing file (the
invariant the MCP server depends on).

[`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) — 8 new
tests via the existing `_FakeSandbox` pattern + two new stub classes
(`_AttachOKSandbox`, `_AttachFailSandbox`) for the
`Sandbox.attach()`-via-monkeypatch path. Coverage:

- `_persist_session` writes the JSON descriptor with the right fields.
- `_delete_session_file` removes the file and is idempotent (it has to
  be — both `finish_task` and `abort_task` call it, and crash recovery
  paths might double-call).
- `_count_agent_commands` recovers the counter from a real trajectory.
- `_rehydrate_sessions`:
  - drops restricted-mode descriptors with a stderr warning
  - drops descriptors whose container is gone (attach raises)
  - rebuilds a session cleanly when the container is alive and the
    trajectory exists
  - returns `{}` when no `sessions/` dir exists (fresh deployment)
  - drops malformed JSON files with a warning (and removes them so
    they don't keep tripping us)

No tests for the `start_task` → file-written or `finish_task` → file-
deleted integration because those just call the helpers we already
test. Adding mock-Docker integration tests would duplicate coverage
without raising the confidence floor.

## Verification

- `uv run pytest` → 123 passed (was 113; +10 new).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
.gitignore                              |    3 +
docs/devlog/0017-session-persistence.md | ~180 +
src/prehnite/mcp_server.py              | ~135 +
src/prehnite/sandbox.py                 |   28 +
src/prehnite/trajectory.py              |   29 +
tests/test_mcp_server.py                | ~165 +
tests/test_trajectory.py                |   30 +
```

`runner.py`, `cli.py`, `egress_proxy.py`, `schemas.py`, the example
tasks, and the existing MCP tool surface are all untouched. The
`build_server(sessions=None)` signature change is backward-compatible
(callers that pass a dict still bypass rehydration).
