# 0001 — MCP wired and driven by a real agent

**Date:** 2026-05-15
**Status:** ✅ end-to-end working

## What

`prehnite-mcp` is now registered with Claude Code via `.mcp.json` at the project
root, and a headless `claude -p` invocation drove the `fix_off_by_one` task
end-to-end through the MCP server. The agent investigated the workspace, found
the bug, fixed it, and called `finish_task` — verify passed. The trajectory at
`trajectories/fix_off_by_one/20260516T023219Z.jsonl` is the artifact.

This is the first time the spine described in CLAUDE.md ran with all four real
participants in the loop: a real agent, a real MCP transport, a real Docker
sandbox, and a real verifier. Up to this point we'd validated each piece in
isolation — pytest hitting the sandbox, the headless `prehnite run` CLI hitting
the runner, FastMCP responding to a health-check `initialize`. None of those
proved an LLM could drive the protocol.

## The wiring

### `.mcp.json`

`claude mcp add -s project -e PREHNITE_ROOT=<root> -- prehnite <path-to-prehnite-mcp.exe>`
generated this:

```json
{
  "mcpServers": {
    "prehnite": {
      "type": "stdio",
      "command": "...\\.venv\\Scripts\\prehnite-mcp.exe",
      "args": [],
      "env": { "PREHNITE_ROOT": "..." }
    }
  }
}
```

This file is checked in. Anyone cloning the repo into a working `uv sync`'d tree
gets the MCP server registration the moment they open the directory in Claude
Code (after the trust prompt).

### Why `PREHNITE_ROOT`

`mcp_server._root()` falls back to `Path.cwd()` if `PREHNITE_ROOT` is unset.
That's fine for the CLI (you run `prehnite-mcp` from the project root) but
fragile for an MCP client which spawns the server with whatever cwd it happens
to have. Setting `PREHNITE_ROOT` explicitly in `.mcp.json` decouples the server
from caller cwd — the tasks dir and trajectories dir resolve from a known root.

## Gotchas hit

### 1. `claude mcp add`'s variadic env eats the server name

This command:

```
claude mcp add -s project -e PREHNITE_ROOT=... prehnite <exe>
```

fails with `error: missing required argument 'commandOrUrl'`. The `-e` option
is variadic (`<env...>` in the help), so commander.js parses `prehnite` as a
second env var and the executable as a third, leaving nothing for the
positional `commandOrUrl`.

The fix is the POSIX `--` separator:

```
claude mcp add -s project -e PREHNITE_ROOT=... -- prehnite <exe>
```

### 2. Standalone `claude.exe` doesn't share auth with your IDE-hosted session

A `claude.exe` invoked from a fresh shell prints `Not logged in · Please run
/login` even when the user is already happily using Claude Code in their IDE.
The IDE wrapper holds an OAuth token in its own process state; the bare CLI
needs its own login (one-time per machine, or set `ANTHROPIC_API_KEY`).

This is annoying for "spawn a worker agent from another agent" workflows and
will probably keep biting until the CLI gets a way to inherit the parent
session's auth. For now: `claude /login` once, then `claude -p ...` works.

### 3. `claude.exe` lives in two places

On this machine the binary exists at *both*:

- `%APPDATA%\Claude\claude-code\<version>\claude.exe` — the user-data install
- `%USERPROFILE%\.vscode\extensions\anthropic.claude-code-<version>-win32-x64\resources\native-binary\claude.exe` — the VS Code extension's bundled copy

The VS Code extension version updates more often (we saw 2.1.143 next to a
stale 2.1.138 in `%APPDATA%`). Either works for `/login` and `-p`, but the
extension copy is the one to prefer.

## The agent's actual trajectory

10 events, monotonic seq, all the right types in the right order:

```
seq=0  run_started      task=fix_off_by_one container=734bdd79b5ca network=false
seq=1  setup_command    rm -rf /workspace/*                            exit=0
seq=2  setup_command    cat > /workspace/count.py <<'PY' ...           exit=0
seq=3  setup_command    printf 'a\nb\nc' > /workspace/data.txt         exit=0
seq=4  agent_command    cat /workspace/count.py                        exit=0
seq=5  agent_command    cat /workspace/data.txt                        exit=0
seq=6  agent_command    cat > /workspace/count.py << 'EOF' ...         exit=0  ← the fix
seq=7  agent_command    cd /workspace && python3 count.py              exit=0  stdout="3\n"
seq=8  verify_command   test "$(python3 /workspace/count.py)" = "3"    exit=0
seq=9  run_finished     result=passed
```

The agent did exactly what a competent human would: read both files first, then
overwrote `count.py` with a corrected version, sanity-checked by running the
script before calling `finish_task`. It used `len(text.splitlines())` instead
of `text.count("\n") + 1` — semantically identical for this task but more
robust to edge cases (empty file, multiple trailing newlines). That choice is
in the trajectory and would be visible to anyone training on these runs.

## What this validates about v0's design

- **Session-based MCP shape** (`start_task` → `exec` → `finish_task`) was the
  right call. The agent benefited from being able to interleave investigation
  and modification — `cat count.py`, `cat data.txt`, then write the fix. A
  single-shot `run_task(commands=[...])` would have forced it to commit to a
  command list before seeing the workspace.
- **`network: false` by default** survived the test — the container had no
  network, the agent didn't need any, the task ran in 21 seconds wall-clock.
- **Append-only JSONL** is a real pleasure to work with. `read_trajectory()`
  reproduces the entire run; the file is `tail -f`-able while a run is live;
  the schema lets us reconstruct who said what to whom in order.
- **Pydantic at the boundaries** caught nothing here because everything was
  well-formed, but the cost has been zero. The runtime overhead of validating
  every event before write is negligible compared to a Docker exec round-trip.

## What's still brittle

- **Single-process session state.** All session dicts live in the
  `mcp_server.build_server()` closure. If the MCP server crashes mid-run, the
  trajectory file is sealed at whatever the last write was, but there's no way
  to resume. For v0 that's fine; eventually we'll want session state persisted
  to disk so a crashed server can be replaced and the agent can `finish_task`
  from a new process.
- **Container leaks on hard kill.** `Sandbox.stop()` runs in a `finally`, but
  if the MCP process is `SIGKILL`'d the container outlives it. We rely on
  Docker's own `auto_remove` being **off** (a deliberate choice so failures
  are inspectable), which means an orphan container per crashed run. A reaper
  script keyed on a label set at create time would close this loop.
- **No timeout on `exec`.** The task spec has `timeout_seconds: 120` but the
  runner doesn't enforce it on individual `exec` calls — a wedged command in
  the agent phase will hang the session until the agent gives up or the MCP
  client disconnects.
- **Output truncation isn't on the radar yet.** A command that prints 10MB of
  stdout will write 10MB into the JSONL file and into the agent's tool result.
  Eventually we'll want a per-event size cap with overflow stored separately.

## Reproducing

```powershell
# in PowerShell, in the project root, with .venv synced and Docker Desktop up
$claude = 'C:\Users\sora5\.vscode\extensions\anthropic.claude-code-2.1.143-win32-x64\resources\native-binary\claude.exe'
$tools = 'mcp__prehnite__list_tasks,mcp__prehnite__start_task,mcp__prehnite__exec,mcp__prehnite__finish_task,mcp__prehnite__abort_task'
& $claude -p --allowed-tools $tools --model sonnet 'Drive prehnite fix_off_by_one end-to-end and report the result.'
```

The `.mcp.json` in the repo root supplies the rest.
