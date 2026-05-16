# 0020 — Functional sandbox: `fork` and `revert`

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

The user framed CLAUDE.md's new "Programming Paradigms" section as:

> **Reflective Programming.** This gives AI agents the ability to see.
> **Functional Programming.** This gives AI agents the ability to act.

`note` (write) + `read_trajectory` (read) covered the reflective half
in [devlog 0019](0019-read-trajectory-tool.md). This commit covers the
functional half: agents can now snapshot the container state and roll
back to it. That's FP's "no destructive mutation" applied to the
sandbox — agent actions become reversible.

Concrete use case the agent now has:

```
sid = start_task(...)
snap = fork(sid)                       # "save point"
exec(sid, "rm -rf /workspace/src")     # oops
revert(sid, snap)                      # back to before the rm
exec(sid, "cp -r /workspace/src.bak /workspace/src")  # do it right
finish_task(sid)
```

Without revert that `rm` would have cost a full task restart. With it
the agent recovers in one tool call.

## How

**`Sandbox.snapshot()`** uses `docker commit` to produce an image
tagged `prehnite-snapshot:<uuid>` with label `prehnite.snapshot=true`.
Returns the snapshot id (the uuid). The MCP server's `fork` tool also
adds a `prehnite.session_id` label so the reaper can later find
orphans whose session is gone.

**`Sandbox.revert(snapshot_id)`** stops + removes the current
container, creates a new one from the snapshot image with the same
container kwargs (network mode, env, labels, resource limits), starts
it, swaps the Sandbox's `_container` reference. Returns the new
container id. The egress proxy (for `restricted` mode) keeps running
across the swap, so the new container reaches it at the same
`HTTP_PROXY` URL.

**`Sandbox.delete_snapshots()`** removes every snapshot image the
sandbox created. Called from `stop()` so per-session snapshots can't
outlive the session, regardless of whether the caller is the MCP
server (finish_task / abort_task) or the headless runner.

**`fork` MCP tool** writes a `session_forked` event to the trajectory
with the snapshot id and the container id at fork time.

**`revert` MCP tool** writes a `session_reverted` event with the
snapshot id, the previous container id, and the new container id —
explicit timeline marker so a future trajectory reader sees the
container_id discontinuity rather than being confused by it. After
revert, it refreshes the session descriptor (devlog 0017) so an MCP
restart attaches to the *new* container.

## The container-kwargs refactor

`start()` previously had ~50 lines computing the docker `create()`
kwargs inline. Both `start()` and `revert()` need the same kwargs
(just with a different image), so I factored a `_make_container_kwargs(image=None)`
helper. `start()` does the egress-proxy setup separately (since revert
reuses the existing proxy) and then both call the helper.

This was a real refactor — 50 lines of inline code became a helper +
two call sites — but it shrinks the surface area meaningfully and was
unavoidable for revert to work. Verified the existing
`test_sandbox.py::test_network_disabled_by_default` and other
integration tests still pass.

## Why `docker commit` and not CRIU

`docker commit` captures the container's filesystem layer only — file
changes, not running processes or memory. CRIU (Checkpoint/Restore In
Userspace) captures everything but it's alpha, Linux-only, doesn't
work reliably on Docker Desktop. For agent workloads, the
filesystem-only capture is sufficient — agents rarely leave
long-running background processes; their state is in `/workspace`
plus possibly `/usr/local/lib/python3.12/site-packages/...` from
`pip install`, and `commit` captures both.

Cost is the trade: ~1–5 seconds per snapshot depending on container
size. The tool docstring tells the agent to pick snapshot points
deliberately (before something risky) rather than snapshot per
command.

## What was deliberately not done

- **No process state capture.** See above.
- **No cross-session snapshot persistence.** Snapshots die when the
  session dies. A future "save snapshot to a named registry the next
  session can load" is a different feature; deferred.
- **No explicit `delete_snapshot` tool.** Snapshots are auto-cleaned
  on session end; agent doesn't need fine-grained control.
- **No `list_snapshots` tool.** The agent already knows its snapshot
  ids (it created them via `fork()` and we returned the id). Asking
  the server for the list would be redundant; the agent can track in
  `note` if it really cares.
- **No revert-counts-as-activity logic.** `agent_command_count`
  stays untouched by fork/revert. They're control-flow, not commands.
  A session that only forks/reverts (no exec) still hits the "no agent
  activity" verdict — which is correct, the agent didn't actually do
  anything in the world.
- **`finish_task` after revert works normally.** The verify suite runs
  against the post-revert container. No special handling needed; the
  agent's "I'll try a different approach" pattern just works.

## Why no eval-signal concern

You can fork/revert mid-session as many times as you want, but
`finish_task` is one-shot — once it runs, the session is dead, the
trajectory is sealed, no more reverts possible. So the agent can
iterate intelligently ("try A, hit dead end, revert, try B") but
can't "redo" a failed verify. The eval signal stays clean: "did the
agent eventually solve it" is what we measure, and that doesn't
change.

## Reap learned a new trick

`prehnite reap` now also cleans orphan snapshot images. Two new
helpers in `cli.py`:

- `_live_session_ids(root)` reads sessions/*.json for `session_id`
  values (parallel to the existing `_live_session_container_ids`).
- `_find_orphan_snapshot_images(client, live_session_ids)` lists
  images with `label=prehnite.snapshot=true`, drops any whose
  `prehnite.session_id` label still matches a live session.

Snapshot images without a `prehnite.session_id` label (manual
`docker commit`, pre-this-commit artifacts) are also treated as
orphans — can't tell which session they belong to, safer to clean.

Aggregate output line grew from "Reaped N containers and M batch logs"
to "Reaped N containers, S snapshots, and M batch logs."

## Tests

[`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) — 5 new
tests via a new `_SnapshottingSandbox` stub (records snapshot calls +
swaps container_id on revert):

- `fork` returns a snapshot id, records `session_forked` event with
  container id, tags the snapshot with `prehnite.session_id` label
- `revert` swaps container, records `session_reverted` event with
  before/after container ids, refreshes the session descriptor
- `revert` with unknown snapshot_id raises
- `fork` with unknown session_id raises
- Neither fork nor revert touches `agent_command_count`

[`tests/test_cli_reap.py`](../../tests/test_cli_reap.py) — 3 new tests
via extended fake docker client (`_FakeImageCollection.list/remove`):

- Snapshot whose `prehnite.session_id` points at a missing session is
  reaped
- Snapshot whose session is still live (descriptor exists) is kept
- Snapshot with no `prehnite.session_id` label (manual / pre-label)
  treated as orphan and reaped

The existing `test_reap_deletes_stale_batch_logs` had its summary
assertion updated for the new "N containers, S snapshots, M batch
logs" format.

No new integration tests for `Sandbox.snapshot()` / `revert()` against
real Docker — the unit tests cover the contract; an integration test
would cost a real `docker commit` per run (~1–5s) for marginal
confidence over what the mocks already verify.

## Verification

- `uv run pytest` → 147 passed (was 139; +8 new).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
docs/devlog/0020-fork-revert.md | ~225 +
src/prehnite/cli.py             |  ~85
src/prehnite/mcp_server.py      |  ~60
src/prehnite/sandbox.py         |  ~155
src/prehnite/schemas.py         |    2 +
tests/test_cli_reap.py          |  ~120
tests/test_mcp_server.py        |  ~180
```

The functional-paradigm half is now in place. The Programming
Paradigms section in CLAUDE.md has two checkboxes checked: read
(reflect) and act (revert).
