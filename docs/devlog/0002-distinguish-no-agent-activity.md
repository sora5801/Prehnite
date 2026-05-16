# 0002 — Distinguish "agent gave up" from "agent tried and failed"

**Date:** 2026-05-15
**Status:** ✅ shipped

## What

Trajectories where the agent issued zero `agent_command` events now end with

```
{"result": "failed", "reason": "no agent activity (verify ran on untouched workspace)"}
```

instead of the previous

```
{"result": "failed", "reason": "verify failed: ['<cmd>', ...]"}
```

The `result` field is unchanged — `failed` still means failed, and the binary
training signal is intact. Only the human-readable `reason` shifts so a
filter can tell "agent never tried" apart from "agent tried and missed."

## Why

Filtering training data is the whole point of the trajectory format. Until
this change, you couldn't tell from `run_finished` alone whether the agent
worked the task. Both these trajectories looked identical at the tail:

- A real attempt that genuinely missed the bug.
- An agent that started the task, did nothing, and called `finish_task`
  immediately (or in our headless case, was launched with `agent_commands=[]`).

A real example surfaced the asymmetry:
[trajectories/fix_off_by_one/20260516T024010Z.jsonl][example] — setup → verify
→ failed, no `agent_command` events at all. Indistinguishable from a hard
attempt that failed by `result`/`reason` alone. You had to read the whole
file to figure out that nothing happened in the middle.

## Implementation

Two counter fields, two `reason` selection branches, one new test file.

### `_Session` gets `agent_command_count: int = 0`

In [`src/prehnite/mcp_server.py`][mcp]. The `exec` MCP tool bumps it after
each successful `writer.write("agent_command", ...)`. `finish_task` then
chooses the reason via three-way:

```python
status = RunStatus.PASSED if not verify_failures else RunStatus.FAILED
if status is RunStatus.PASSED:
    reason = "all verify checks passed"
elif sess.agent_command_count == 0:
    reason = "no agent activity (verify ran on untouched workspace)"
else:
    reason = f"verify failed: {verify_failures}"
```

### `RunContext` gets `_command_count: int = 0`

In [`src/prehnite/runner.py`][runner]. This is the runner-side equivalent
for the headless `run()` path used by the CLI and tests.

`RunContext.exec()` increments `self._command_count` after the underlying
`_exec_and_record(...)` returns. `run()` initialises a local
`agent_command_count = 0` before the agent phase, then either reads it from
`ctx._command_count` (when an agent callback was passed) or bumps it inline
(when `agent_commands` was passed).

The reason override mirrors the MCP version exactly. The two code paths
deliberately don't share a helper — they're four lines each and a helper
would obscure rather than clarify.

### `RunContext` is no longer `frozen=True`

This was the only "structural" change. The counter has to live somewhere
the increment can reach; mutating a field on a frozen dataclass requires a
proxy (a one-element list, an `object.__setattr__` shim, etc.). All of
those are uglier than just removing `frozen=True`.

`RunContext` isn't placed in any sets or dicts and isn't compared, so the
hashability/equality semantics aren't load-bearing. The original `frozen`
was defensive style, not load-bearing — it's gone now.

## What was deliberately not done

- **No new event type.** The contract is: `result` is the binary signal,
  `reason` is the human-readable annotation. Adding `gave_up` or similar
  would have rippled through `EventType`, `RunStatus`, schema docs, and any
  downstream consumer reading the JSONL. We have none yet, so the cost was
  small, but the principle holds — the v0 schema has been working and the
  bar to extend it should be high.
- **No timeout-based detection.** "No agent activity" here means "zero
  exec calls", not "exec calls but they all timed out" or "the session sat
  idle for N seconds". Time-based heuristics are the right thing eventually
  but they're a separate change.
- **`abort_task` was untouched.** Its reason (`"aborted by agent"`) is
  already unambiguous — there's no failure-vs-gave-up confusion possible.
- **No fix to the pre-existing mypy errors in `sandbox.py`.** Two
  `name-defined`/`attr-defined` errors against the `docker` package's
  type-stub-less surface. Confirmed via `git stash` that they exist on
  the previous commit; my edits introduce zero new mypy errors. These
  belong in their own change — probably either a `types-docker` install
  or strategic `# type: ignore` comments at the docker call sites.

## Tests

[`tests/test_runner.py`][tests] is new. It substitutes a `_FakeSandbox`
into `prehnite.runner.Sandbox` via `monkeypatch`, so neither test touches
Docker. Two assertions, both deliberately narrow:

- `agent_commands=[]` + a verify command of literal `"false"` (which the
  fake reports as exit 1) → reason contains `"no agent activity"` and not
  `"verify failed"`.
- `agent_commands=["echo something"]` + the same failing verify → reason
  contains `"verify failed"` and not `"no agent activity"`.

Each test also reads back the trajectory file to confirm the `run_finished`
event itself carries the correct reason, not just the in-memory `RunResult`.
The trajectory is the source of truth for downstream consumers; checking
both prevents a future refactor from silently desynchronizing them.

The MCP server's parallel logic is *not* unit-tested in this commit — it's
the same three-way branch with `sess.agent_command_count` instead of the
local int. Both paths exercise the same shape, and adding a parallel test
would just duplicate the runner test against a stubbed FastMCP. Worth
revisiting if the two paths diverge in the future.

## Verification

- `uv run pytest` → 30 passed (28 prior + 2 new).
- `uv run mypy src/prehnite` → 2 errors, both pre-existing in
  `sandbox.py`, none introduced.
- `uv run prehnite run tasks/examples/hello.yaml`
  → `status: failed, reason: no agent activity (verify ran on untouched workspace)`.
- `uv run prehnite run tasks/examples/hello.yaml --cmd 'echo hi > /workspace/hello.txt'`
  → `status: passed, reason: all verify checks passed`.

## Diff size

```
src/prehnite/mcp_server.py |  9 +++++++--
src/prehnite/runner.py     | 16 ++++++++++++----
tests/test_runner.py       | 70 ++++++++++++++++++++++++++++++++++++++
3 files changed, 89 insertions(+), 6 deletions(-)
```

Behavior tweak, not a refactor.

[example]: ../../trajectories/fix_off_by_one/20260516T024010Z.jsonl
[mcp]: ../../src/prehnite/mcp_server.py
[runner]: ../../src/prehnite/runner.py
[tests]: ../../tests/test_runner.py
