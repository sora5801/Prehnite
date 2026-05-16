# 0021 — Reflective × functional: branch-aware `read_trajectory`

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

`read_trajectory` (devlog 0019, reflective) and `fork`/`revert`
(devlog 0020, functional) interact implicitly: the trajectory is
append-only, so after a revert the agent's `read_trajectory` call
returns events from *both* the discarded branch and the new one,
with a `session_reverted` event as the boundary.

That works, but a multi-fork session produces a linear log that
mixes "what actually happened" with "what was rolled back." If the
agent does N fork/revert cycles, the log has N abandoned branches
interleaved with the surviving timeline. Hard to skim.

This commit adds a `branch` parameter to `read_trajectory` so the
agent can filter to the timeline it cares about.

## API

```
read_trajectory(session_id, since_seq=0, branch="all")
```

Three branch values:

- **`"all"`** (default): every event in the trajectory. Same as before
  this commit. Useful when the agent wants the complete history.
- **`"current"`**: events on the path that wasn't rolled back. Drops
  events strictly between any `session_forked` and its matching
  `session_reverted`. The fork/revert events themselves are kept —
  they ARE part of what really happened. Useful in multi-fork
  sessions when the agent wants to see "the timeline as it stands."
- **`"<snapshot_id>"`**: events on one specific branch — from its
  fork up to its matching revert (or to end if not yet reverted).
  Useful for "what did I learn trying approach A?" recall after
  reverting.

Composes with `since_seq` for incremental polling on a filtered
view.

## How branches are computed

At read time, walk the events to build:

1. `fork_seq: dict[snapshot_id → seq]` — where each fork happened.
2. `discarded: dict[snapshot_id → (fork_seq, revert_seq)]` — for each
   revert that references a known fork, the seq range that was
   discarded.

Then:

- `branch="current"`: union all `(fork_seq+1, revert_seq)` ranges into
  a `discarded_seqs: set[int]`; return events whose seq is not in it.
- `branch="<snap_id>"`: if the snapshot was reverted, return events
  whose seq is strictly between `fork_seq[snap_id]` and the matching
  `revert_seq`. If the snapshot was forked but not reverted, return
  events after the fork (it's still the live continuation). Unknown
  `snap_id` returns `[]`.

Pure function — no state, no I/O beyond reading the trajectory. Tests
seed synthetic trajectories and assert the filtered output directly.

## Edge cases handled

- **Snapshot never reverted**: `branch=<snap_id>` returns everything
  after the fork. The snapshot is "still live" — its branch IS the
  current path.
- **Same snapshot reverted twice**: the later revert wins for the
  range computation. Earlier discarded events stay discarded by the
  later range. (Reverting to the same point twice is unusual but
  legal.)
- **Empty trajectory**: returns `[]` regardless of branch.
- **`session_forked` / `session_reverted` events themselves**: always
  in the "current" branch. They mark the boundary; they aren't part
  of the discarded content.
- **Nested forks/reverts**: each fork/revert pair is treated
  independently. An event inside an inner reverted branch is
  discarded (by the inner pair) AND inside any outer reverted branch
  (by the outer pair). Union semantics handle both correctly.

## What's deliberately not done

- **No "branch graph" tool.** An agent that wants to understand the
  fork/revert structure can call `read_trajectory(branch="all")` and
  walk the `session_forked`/`session_reverted` events themselves. The
  data is already there; no need for a separate API.
- **No new event type.** Branches are computed from existing
  fork/revert events; no schema change.
- **No per-event "branch_id" stored on the event.** It's derived on
  read, not persisted. Keeps the trajectory format unchanged. Adding
  per-event tagging would be more efficient for very large
  trajectories but is over-engineering for v1 (current trajectories
  are kilobytes; the filter walks them in microseconds).

## Tests

[`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) — 6 new
tests via a `_write_branchy_trajectory(path)` helper that lays down
a deterministic shape (pre-fork → fork → 2 events on branch A → revert
→ 1 event on branch B):

- `branch="all"` returns every agent_command including discarded ones
- `branch="current"` excludes branch-A events; keeps fork/revert
  markers
- `branch="snap-A"` returns ONLY the discarded events
- Unknown snapshot id returns `[]`
- Fork without revert: `branch=<snap_id>` returns events after the
  fork (it's the live continuation)
- `branch` composes with `since_seq` for incremental reads on a
  filtered view

## Verification

- `uv run pytest` → 153 passed (was 147; +6 new).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
docs/devlog/0021-branch-aware-reads.md | ~125 +
README.md                              |    5 +
src/prehnite/mcp_server.py             |   85 +
tests/test_mcp_server.py               | ~155 +
```

The MCP tool surface stays at 10 tools; only `read_trajectory` grew
a parameter. Reflective × functional combination now has a deliberate
API rather than just an emergent one.
