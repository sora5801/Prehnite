# 0019 — In-session reflection: `read_trajectory` MCP tool

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

The `note` tool from [devlog 0006](0006-agent-thoughts.md) gave the
agent a way to *write* its reasoning into the trajectory. The inverse
direction was missing: the agent couldn't read back what it had
written, or recall the stdout of a command it ran ten exec-calls ago.
For a non-trivial task the agent ends up re-running `cat` to remind
itself what a file contains; for a long task it loses track of what
it noted.

This commit adds `read_trajectory(session_id, since_seq?)` — the
agent reads its own in-flight events back, mid-session.

## Tool API

```
read_trajectory(session_id: str, since_seq: int = 0)
    -> list[event]  # each event has seq, ts, type, data
```

Returns every event in the session's trajectory whose `seq >= since_seq`.
Default `since_seq=0` returns everything; pass a higher value to fetch
only what's new since the agent's last read (so it can poll without
bloating context).

Includes setup commands too — the agent gets to see what was done for
it before its turn started, not just its own actions.

## What it does NOT do

- **Doesn't count as agent activity.** `agent_command_count` is
  untouched. A session whose only agent action is reading the
  trajectory still hits the "no agent activity (verify ran on
  untouched workspace)" verdict on failure
  ([devlog 0002](0002-distinguish-no-agent-activity.md)). Reading
  isn't acting.
- **Doesn't write an event of its own.** No `agent_read` event in the
  trajectory. Reading is observation, not action; the trajectory
  records what happened *in the world*, not what the agent looked at.
- **Doesn't filter by event type.** Returns everything in seq range.
  Filtering is the agent's job at the LLM layer — they're already
  parsing free-form text, picking events from a list is trivial.
- **Doesn't bound size.** A session with megabytes of stdout could
  return a megabyte payload. In practice trajectories are kilobytes;
  if this becomes a real problem, `since_seq` is already the escape
  valve. Future option: add `max_bytes` parameter.

## The since_seq design

Without `since_seq`, an agent that polls trajectories ("did anything
happen?") would get exponentially growing payloads back as the
session progresses. The agent's context window bloats fast — after
five reads of a 20-event trajectory, it's seen 100 events worth of
text.

`since_seq` lets the agent track its own cursor. Sample agent loop:

```
last_seen = 0
while not done:
    events = read_trajectory(sid, since_seq=last_seen + 1)
    for e in events:
        ... process ...
        last_seen = e.seq
    ... take next action ...
```

A polite agent that uses `since_seq` makes O(events) total tokens
across all reads. A rude agent that re-reads everything makes
O(events²). The tool description names this so the LLM-side picks
up the pattern.

## Why this is the read counterpart to `note`

`note` writes free-form text into the trajectory at the agent's
discretion. `read_trajectory` reads any of it back — including the
agent's own prior notes, plus setup, plus command outputs.

Together they make trajectories into a working scratchpad: write what
you're thinking, recall what you wrote (and what happened) later.
That's the v1 of "reflective" — read-only, in-session, no cross-run
memory. Cross-run memory would muddy the eval signal (an agent that
passed because it remembered yesterday's answer is a different signal
than "figured it out") so it's deliberately deferred until we have
the eval infrastructure to separate the two populations.

## Implementation notes

The tool body is ~5 lines:

```python
sess = _require(sessions, session_id)
events = _read_trajectory_file(sess.trajectory_path)
return [e.model_dump(mode="json") for e in events if e.seq >= since_seq]
```

`_read_trajectory_file` is the existing `read_trajectory` from
`prehnite.trajectory`, just renamed on import so the tool function
(also named `read_trajectory`) doesn't shadow it. Tested cross-
reference paths (the `_count_agent_commands` helper from session
persistence) updated to use the same renamed import.

Race-safety with the egress-proxy thread: the writer holds a `Lock`
around its append+flush
([devlog 0008](0008-network-policy.md)), so events are committed
atomically per line. The read path opens the file separately and
parses line-by-line. A partial line (writer mid-flush during a read)
*could* fail `read_trajectory`'s pydantic validation. Hasn't been
observed in practice; documented as a known limitation. If it
becomes a real problem, switch the reader to a lenient mode that
skips malformed final lines.

## Side effect: `_MCP_TOOL_NAMES` grows

The constant in `cli.py` that backs `{tools}` substitution for
`prehnite batch --agent` gains `read_trajectory`. Anyone using batch
with `{tools}` automatically gets the new allowed-tool entry on
their next run — no changes to existing agent templates required.

## Tests

[`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) — 5
new tests via the existing `_seed_session` + `call_tool` pattern:

- Returns every event written (setup + agent commands + thoughts).
  Verifies the agent's earlier stdout AND its earlier note are both
  present in the returned list — those are the "what did I do?" and
  "what did I think?" recall cases.
- `since_seq=N` filters out events with seq < N.
- Reading does not bump `agent_command_count` (locks in the
  no-activity carve-out).
- Unknown `session_id` raises (same as every other session-keyed tool).
- Empty trajectory returns an empty list rather than crashing.

## Verification

- `uv run pytest` → 139 passed (was 134; +5 new).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
docs/devlog/0019-read-trajectory-tool.md | ~145 +
README.md                                |    5 +
src/prehnite/cli.py                      |    1 + (tool name in _MCP_TOOL_NAMES)
src/prehnite/mcp_server.py               |   33 +
tests/test_mcp_server.py                 | ~100 +
```

`runner.py`, `sandbox.py`, `egress_proxy.py`, `schemas.py`,
`trajectory.py`, the example tasks: untouched.

## What this enables next

The next natural extension is cross-run learning — let the agent
read `prior_trajectories_for_task(task_id)` at the start of a new
session. The mechanism would be similar (a new MCP tool calling
into the existing `stats`/`inspect` infrastructure) but the design
call to make first is around eval-signal hygiene: tag runs as
"with memory" vs "without" in `stats` so the two populations stay
separable. Deferred for now.
