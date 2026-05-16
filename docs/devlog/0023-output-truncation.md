# 0023 — Bounded stdout/stderr with content-addressed overflow

**Date:** 2026-05-16
**Status:** ✅ shipped

## Why

Until this commit, a single noisy command — `pip install -v`, a verbose
test run, a `cat` of a generated CSV — could shove megabytes into one
`agent_command` event. Three things break when that happens:

1. **The JSONL trajectory inflates** by a factor of ~1000. A 200-line
   run that should be 30 KB becomes 30 MB. `prehnite inspect` chokes
   loading it.
2. **`read_trajectory` shoves the same bytes back at the agent**, who
   asked for "every event so far" and got a 30 MB string. The agent's
   context window evaporates before it can do anything useful.
3. **MCP's `exec` return value carries the full output too** — the
   agent's *immediate* response is what poisons it fastest. The
   agent runs one command, takes the full output as its tool result,
   and is suddenly a couple of pages from context-exhaustion before
   ever calling `read_trajectory`.

This commit bounds all three at the same chokepoint: the trajectory
writer caps stdout and stderr on command events, spills the full bytes
to a content-addressed overflow file on disk, and the MCP `exec` tool
returns the post-cap form so the agent's response is bounded too.

## Design

```
sandbox.exec(cmd) ─► CommandResult{stdout: 5MB, stderr: 0}
                       │
                       ▼
            writer.write("agent_command", result.model_dump())
                       │
                       │ if event_type in {setup, agent, verify}_command
                       │ and overflow_dir is set:
                       │
                       ▼
               cap_command_data(data, max_bytes=8KB, overflow_dir=…)
                       │
                       ▼
          {stdout: "<8KB head>", stdout_truncated: true,
           stdout_overflow_sha256: "<hex>",
           stdout_original_bytes: 5_242_880,
           stderr: ""}
                       │
                       ▼
             trajectory.jsonl (line ≤ 9 KB)
                       │
                       ▼
        event.data ─► returned to agent via exec tool
```

The full 5 MB lives at `<root>/overflow/<sha256>` until manually cleaned.

## Five design calls

The user approved all five in the planning round; in order:

### 1. Head-only truncation, not head+tail

Most actionable signal in command output is at the top: the first
error, the stack trace's leaf frame, the failing assertion. Test
runners that summarize at the end (pytest, jest) usually fit
comfortably under 8 KiB anyway; if they don't, the agent can `cat
overflow/<sha>` (or a future `read_overflow` tool) to retrieve the
tail. Head+tail would have doubled the bookkeeping (two cut points,
two byte budgets) for marginal benefit.

### 2. 8 KiB default cap per stream, no per-task configurability for v0

8 KiB ≈ 100 lines of typical CLI output — enough for a real
diagnostic but small enough that an agent can call `exec` 30 times
without filling its context. The writer accepts a `max_stream_bytes`
constructor arg so tests and future callers can override it, but no
YAML knob in `Task`. If we discover real tasks where 8 KiB is wrong,
that's the right time to add the knob — not before.

### 3. Overflow files persist beyond session

They're cheap (sha256-named, dedupe across runs) and they're the
ground truth for a reviewer trying to understand what the agent
actually saw. Auto-cleanup would be a footgun — the moment an
investigator needs the full bytes, they're gone. `prehnite reap`
could grow an `--overflow-older-than` flag later if disk fills up.
For v0, the `/overflow/` directory just accumulates and is gitignored.

### 4. No new `read_overflow(sha)` MCP tool yet

The agent rarely needs the full bytes (head + truncation marker is
usually enough context to know what happened). When it does, the
plain filesystem path inside the container would work — but the
overflow dir lives on the host, not in the container. Adding the MCP
tool is straightforward when a real task demands it; until then it's
speculative.

### 5. No schema change — metadata as extra keys in `event.data`

`TrajectoryEvent.data` is `dict[str, object]` (no extra-field
forbid). Adding `stdout_truncated`, `stdout_overflow_sha256`,
`stdout_original_bytes` as siblings of `stdout` keeps the
trajectory format unchanged and any existing reader that only looks
at `stdout` still works (they just see a shorter string). A typed
`TruncatedStream` model would force every event consumer to know
about overflow; flat extra keys mean "if you don't care, ignore."

## What gets capped, and where

Capping is scoped narrowly:

- **Event types**: only `setup_command`, `agent_command`,
  `verify_command`. `agent_thought` text is self-limited by the
  agent's own writing; `egress_attempt` carries no streams;
  `session_forked`/`session_reverted` are tiny.
- **Fields**: only `stdout` and `stderr`. `cmd` is bounded by what
  the agent typed (or what the task author wrote in setup/verify);
  capping it would be misleading.
- **Activation**: only when the writer is constructed with
  `overflow_dir=...`. The default constructor (no `overflow_dir`)
  preserves full output, which is what existing tests and any
  caller wanting raw bytes get. Both production callsites
  (`runner.run` and the MCP server's `start_task` /
  `_rehydrate_sessions`) pass `overflow_dir=root / "overflow"`.

## UTF-8 boundary handling

Naive truncation at byte `N` can land mid-multibyte and produce a
mojibake head. The cap walks back from `N` until the byte is a
UTF-8 start byte (top two bits != `10`), so the returned head always
decodes cleanly. Tested with `"あ" * 4` (3 bytes per char) at
`max_bytes=4` — we get a clean one-character head, no `�`.

The overflow file holds the full original bytes regardless; only the
in-trajectory head is boundary-aligned.

## Dedupe

Content-addressing means identical outputs across commands write one
overflow file. A loop printing the same 100 KB blob 50 times produces
fifty trajectory events all pointing at one `<sha256>` file. Cheap
property of sha256 — no extra bookkeeping.

## Tests

- **`tests/test_overflow.py`** (new, 8 tests): cap_stream small/large
  paths, dedupe, sha256 matches full original, UTF-8 boundary,
  cap_command_data caps both streams independently, leaves small
  streams alone, doesn't mutate caller dict.
- **`tests/test_trajectory.py`** (3 new tests):
  - Writer caps command-event streams when `overflow_dir` is set
    (head ≤ cap, overflow file written, JSONL on disk also truncated)
  - Writer leaves non-command events (`agent_thought`) untouched
    even at huge sizes
  - Default construction (no `overflow_dir`) preserves full output
- **`tests/test_mcp_server.py`** (1 new test): exec returns the
  truncated dict to the agent and spills the full bytes to overflow,
  end-to-end through the FastMCP `call_tool` path

## Verification

- `uv run pytest` → 165 passed (was 153; +12 new).
- `uv run mypy src/prehnite` → clean.

## Diff size

```
.gitignore                                   |    5 +
README.md                                    |   10 +
docs/devlog/0023-output-truncation.md        | ~140 +
src/prehnite/mcp_server.py                   |   13 +-
src/prehnite/overflow.py                     |  110 +
src/prehnite/runner.py                       |   12 +-
src/prehnite/trajectory.py                   |   27 +-
tests/test_mcp_server.py                     |   71 +
tests/test_overflow.py                       |  120 +
tests/test_trajectory.py                     |   62 +
```

## What this unblocks

- **Long-form agent runs** are now bounded. The trajectory grows at a
  predictable rate regardless of how chatty the commands are.
- **`prehnite stats` over many trajectories** stays usable; no single
  noisy trajectory bloats aggregate parsing.
- **The agent's context budget is protected** in two places: the
  immediate `exec` return value AND `read_trajectory` recall. Both
  see the same capped form.

## What's deliberately not done

- **No `read_overflow(sha)` MCP tool.** Defer until a task needs it.
- **No per-task `max_stream_bytes` in `Task`.** Defer until a task
  needs it.
- **No retro-cap of existing trajectories.** Only new writes are
  capped; old trajectories keep whatever bytes they had. (`prehnite
  inspect --full` was already a way to deal with verbose old runs.)
- **No GC for overflow files.** They accumulate; gitignored; `reap`
  could grow a knob later.

This closes the last v0-brittleness item from devlog 0001's list —
the eval harness now stays well-behaved under realistic workloads.
