# 0006 — Agents can record their reasoning

**Date:** 2026-05-15
**Status:** ✅ shipped, end-to-end verified with a real agent

## What

A new MCP tool — `note(session_id, thought)` — lets the agent write a free-text
event into the trajectory between commands. The event type `agent_thought` is
added to the schema; ordering against shell commands is preserved by the
existing `seq` counter; counts of `agent_command` are unchanged when an agent
calls `note` (a thought is not an action).

## Why

v0 captured shell-level events only by design — fine choice for "what
happened", but it left no place at all for the agent's *thinking*. Compare
two `fix_off_by_one` trajectories from the same agent on the same task:

**Before (v0):** [20260516T020146Z.jsonl](../../trajectories/fix_off_by_one/20260516T020146Z.jsonl)

```
setup → cat → cat → write count.py → run → verify → passed
```

You can replay the commands but you can't tell *why* the agent picked
`len(text.splitlines())` over `text.count("\n") + 1`, or whether it was
just guessing. For training/eval that's the more valuable signal.

**After (this commit):** [20260516T035618Z.jsonl](../../trajectories/fix_off_by_one/20260516T035618Z.jsonl)

```
setup → cat (investigate) → THOUGHT → write count.py → run → verify → passed
```

The thought:

> The script counts newline characters with text.count("\n"). data.txt has 3
> lines but no trailing newline after the last line "c", so count("\n")
> returns 2 instead of 3. The fix is to use splitlines() which correctly
> counts lines regardless of trailing newline presence.

That's the entire reasoning chain. Anyone reading the trajectory now knows
the hypothesis the agent acted on, not just the resulting code.

## Design choices and what was deliberately skipped

- **Decoupled.** The `note` tool is independent from `exec`. No `thought`
  argument on `exec`; no `thought` field inside `agent_command` data. Order
  is preserved purely by the existing monotonic `seq`. This keeps both
  events orthogonal — an agent can call `note` zero, one, or many times
  between commands without coupling reasoning to action.
- **Optional, not enforced.** Nothing rejects a session that produced no
  thoughts. The "no agent activity" reason from
  [devlog 0002](0002-distinguish-no-agent-activity.md) still keys on
  `agent_command_count`, not thought count, because thoughts without
  commands aren't a real attempt.
- **MCP-only.** `runner.py` and `cli.py` are untouched — the headless CLI
  doesn't need to record thoughts. It runs fixed command lists; there's no
  agent to do reasoning. If a future agent driver ever bypasses MCP,
  threading thoughts through `RunContext` would be the natural extension.
- **Tool description matters.** `note`'s docstring is deliberately
  invitational: "Record your reasoning here between commands — what you
  tried, why you tried it, what you expect to happen. The more reasoning
  you record, the more useful the trajectory is." Without that wording an
  agent reading the tool list will technically *know about* `note` but
  won't reach for it. The verification run confirms Sonnet 4.6 reaches for
  it naturally with this description, calling it once between investigation
  and fix without any prompt-side nudging.

## One small refactor needed for testability

`build_server()` now accepts an optional `sessions` parameter:

```python
def build_server(sessions: dict[str, _Session] | None = None) -> FastMCP:
```

`None` preserves the previous behaviour exactly (constructs a fresh
empty dict). Passing one in lets a test inject a fake `_Session` without
going through `start_task` (which needs Docker). This is the smallest
change that makes the test the user asked for — "calls `build_server()`,
invokes the `note` tool against a fake session" — actually possible, since
`sessions` was previously a closure variable inaccessible from outside.

## Tests

- [`tests/test_schemas.py`](../../tests/test_schemas.py) — one new
  round-trip test for an `agent_thought` event through
  `model_dump_json` → `model_validate_json`.
- [`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) — new file.
  Two tests, both async (using FastMCP's `call_tool` interface so the test
  exercises the same path a real MCP client would take):
  - `test_note_writes_agent_thought_and_does_not_bump_command_count` —
    seeds a fake session with `agent_command_count=3`, calls `note`,
    asserts exactly one `agent_thought` event landed in the trajectory
    file *and* the counter is still 3.
  - `test_note_unknown_session_raises` — `_require` raises `KeyError` for
    unknown session IDs; FastMCP wraps that so we just assert *something*
    is raised.

## One incidental fix: README encoding

While editing the trajectory-format table I noticed `README.md` had been
re-encoded to UTF-16 LE somewhere in this session — probably an earlier
PowerShell `Set-Content` (defaults to UTF-16 on Windows). Re-wrote it as
UTF-8. The content didn't change beyond adding the `agent_thought` row
to the table and listing `note` in the MCP tool inventory.

## Verification

| | |
| --- | --- |
| `uv run pytest` | 38 passed (35 prior + 1 schemas + 2 MCP) |
| `uv run mypy src/prehnite` | clean |
| Real agent run via `claude -p` | passed; trajectory contains one `agent_thought` event with substantive reasoning |

## Diff size

```
docs/devlog/0006-agent-thoughts.md | 117 +++++++++++++++++++++++++++++++++++
README.md                          |  92 +++++++++++++++++++--------    (+ encoding fix)
src/prehnite/mcp_server.py         |  16 ++++-
src/prehnite/schemas.py            |   1 +
tests/test_mcp_server.py           |  72 ++++++++++++++++++++++
tests/test_schemas.py              |  12 ++++
6 files changed, 290 insertions(+), 20 deletions(-)
```

`runner.py`, `cli.py`, `sandbox.py`, `trajectory.py`, `tasks/loader.py`,
example tasks: untouched.
