# 0022 — `fix_circular_import` task + inspect renderers for fork/revert

**Date:** 2026-05-16
**Status:** ✅ shipped, end-to-end verified

## Why

Devlog 0020 shipped `fork` and `revert`, but no example task
naturally invited their use. This commit adds
[`fix_circular_import`](../../tasks/examples/fix_circular_import.yaml)
— a real bug across three Python files with several plausible
fixes — and verifies a real agent actually reaches for the new
tools.

While doing the end-to-end run, the cosmetic gap I predicted
materialized: `session_forked` events landed at the generic
fallback rendering path in `prehnite inspect` (since devlog 0010
wrote the formatter before these event types existed). Fixed
that too.

## The task

Three files in `/workspace/`:

```python
# user.py
from order import Order
class User: ...

# order.py
from user import User
class Order:
    def __init__(self, user: User, item): ...

# test_app.py
from user import User
# runs a scenario, asserts invariants
```

`python3 test_app.py` raises `ImportError` from the cycle. Several
plausible fixes:

1. Deferred (function-local) `from order import Order` inside
   `User.add_order`.
2. `TYPE_CHECKING` guard on order.py's `from user import User`,
   combined with `from __future__ import annotations` so the
   `user: User` annotation never evaluates at runtime.
3. Refactor into a third module that both depend on.
4. Merge the two classes into one file.

The description nudges the agent toward `fork` ("snapshot before
your risky edit, and if that approach doesn't work, `revert` and
try a different one") but doesn't force it. The verify is a
straightforward `python3 test_app.py | grep -q ok`.

## What the real agent did

Sonnet 4.6 driving via MCP (full trajectory:
[trajectories/fix_circular_import/20260516T121038Z.jsonl](../../trajectories/fix_circular_import/20260516T121038Z.jsonl)):

```
seq=5  THOUGHT  Plan: add `from __future__ import annotations` and
                guard the import with `TYPE_CHECKING`. Only order.py
                needs the fix.
seq=6  agent_command  reproduce the ImportError + cat user.py
seq=7  FORK     snap=88e7ffe071fc  container=5698cbb68546
seq=8  agent_command  rewrite order.py with the fix
seq=9  agent_command  python3 test_app.py  → "ok"
seq=10 THOUGHT  Fix verified, summarize.
seq=11 verify_command  passed
```

**The agent forked defensively, didn't need to revert because the
first approach worked.** Exactly the pattern fork was designed for:
not a recovery tool after failure, but a cheap insurance habit
before a risky edit. The trajectory shows the discipline; future
reviewers see "the agent considered this risky enough to
snapshot."

The agent did NOT call `read_trajectory` in this run. Single fix,
no investigation across stale state — read_trajectory's value
comes more in longer/iterative sessions. The trajectory is the
proof, not the absence-of-use.

Of the four plausible fixes I sketched in the task description,
the agent picked #2 (the most "Pythonic" — preserves type
annotations for static analysis tools without runtime cost). It
applied the fix to one file and tested in one shot. Mature agent
behavior.

## The inspect cosmetic gap

`prehnite inspect` previously rendered fork/revert events via the
generic fallback path:

```
[  7] 12:10:57 session_forked: {'snapshot_id': '88e7ffe071fc4c60950abf0b330258f7', 'container_id': '5698cbb68546963fe91999801698509c9f640d72aa3ac7d8f9b3fa6b4d43db08'}
```

Workable but noisy. Added two dedicated renderers in
[`src/prehnite/cli.py`](../../src/prehnite/cli.py) that truncate
ids to 12 chars and lay out fields cleanly:

```
[  7] 12:10:57 session_forked   snap=88e7ffe071fc  container=5698cbb68546
[ 14] 12:11:23 session_reverted snap=88e7ffe071fc  prev=5698cbb68546 -> new=ce8a2f74b033
```

12-char truncation matches Docker's own `docker ps` short id
length — a human cross-referencing with `docker ps -a` can
match by sight.

## Why the task description suggests fork rather than forcing it

Could have designed a task that physically cannot be solved
without fork/revert (e.g., the right answer requires speculative
exploration that's impossible to reconstruct from memory). But:

- That kind of task feels gamified, doesn't match real software
  work.
- It would over-fit the eval signal to one tool.
- Some agents will solve `fix_circular_import` in one shot
  without forking (less defensive style); that's not wrong,
  it's a real signal about the agent.

By suggesting-but-not-forcing, the trajectory captures the
agent's *judgment* about when to snapshot — which is itself
useful eval data. A future `stats` extension could count
"fork-events-per-task" alongside pass rate.

## Tests

[`tests/test_cli_inspect.py`](../../tests/test_cli_inspect.py) —
the existing `_write_sample_trajectory` fixture grew two more
events (a fork + revert pair) so the existing
`test_inspect_renders_every_event_type` test now also locks in
the new renderers. Added three new assertions to that test:

- `session_forked` line includes `snap=abcdef123456` (truncated
  to 12 chars)
- `session_reverted` line is present
- The format `prev=... -> new=...` shows up

No separate new test file needed; the existing test was the
natural home.

## Verification

- `uv run pytest` → 153 passed (unchanged count — the existing
  test grew assertions but didn't split).
- `uv run mypy src/prehnite` → clean.
- Headless smoke (`prehnite run tasks/examples/fix_circular_import.yaml`):
  no-fix → "no agent activity" verdict; with deferred-import
  fix via --cmd → passed.
- Real-agent end-to-end (`claude -p` via MCP): passed in one
  shot with defensive fork.

## Diff size

```
docs/devlog/0022-fix-circular-import-task.md | ~155 +
src/prehnite/cli.py                          |   18 +
tasks/examples/fix_circular_import.yaml      |   75 +
tests/test_cli_inspect.py                    |   25 +
```

The example-task corpus is now 7 (was 6). Coverage:

| shape | task |
| --- | --- |
| smoke | hello |
| single-file bug fix | fix_off_by_one |
| multi-file investigation | merge_configs |
| `restricted` mode + allow | install_cowsay |
| `restricted` mode + deny | egress_allowlist |
| likely multi-shot | fix_log_stats |
| **multi-file refactor with multiple plausible fixes** | **fix_circular_import** |
