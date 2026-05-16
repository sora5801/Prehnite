# Prehnite

Sandboxed Linux environment with a task runner and trajectory logger, aimed at
developers fine-tuning or evaluating coding agents.

In one sentence: **agents run inside an isolated Docker container, perform coding
tasks, and every action they take is captured as structured JSONL we can replay
or train on.**

## Status

The end-to-end spine works: an agent connects via MCP, drives a task in an
ephemeral Docker container, and a trajectory is written to disk capturing
shell commands, agent-authored reasoning notes (via the `note` tool), and
— when the task opts into `network.mode: restricted` — every network
egress attempt with allow/deny + timing.

Not yet built: web UI, syscall-level capture, multi-container orchestration,
non-Docker sandboxes. See [CLAUDE.md](CLAUDE.md) for the full out-of-scope list.

## Requirements

- Python 3.11+
- Docker daemon reachable from the host (`docker info` works)
- [`uv`](https://docs.astral.sh/uv/) for environment + dependency management

## Quickstart

```bash
# Install in a uv-managed virtualenv
uv sync --extra dev

# Build the base sandbox image (one-time, ~1 min)
docker build -t prehnite-base:latest -f docker/base.Dockerfile docker/

# Headless smoke run — no --cmd means no agent commands, so verify fails with
# reason "no agent activity (verify ran on untouched workspace)". That's
# expected for this invocation and exercises the failure path.
uv run prehnite run tasks/examples/hello.yaml

# Supply an agent command to make verify pass:
uv run prehnite run tasks/examples/hello.yaml \
    --cmd 'echo hi > /workspace/hello.txt'

# Inspect the trajectory
ls trajectories/
```

## MCP server

Prehnite exposes a stdio MCP server. The server publishes these tools:

- `list_tasks(tag?, difficulty?)` — returns matching tasks. Both filters
  optional and AND-combined.
- `describe_task(task_id)` — returns the full task spec for a single task
  (everything in the YAML).
- `start_task(task_id)` — opens a sandbox, runs setup, returns a `session_id`
- `exec(session_id, cmd)` — runs a shell command inside the sandbox
- `note(session_id, thought)` — records the agent's reasoning between commands
- `finish_task(session_id)` — runs verify, writes the final event, tears down
- `abort_task(session_id)` — tears down without running verify

A `.mcp.json` is checked in at the project root, so a Claude Code session
opened in this directory picks the server up automatically after the trust
prompt. Other clients can point at:

```bash
uv run prehnite-mcp
```

## Task format

Tasks are hand-authored YAML. See [tasks/examples/](tasks/examples/) for the
canonical shape. A minimal task:

```yaml
id: hello
description: Create a hello.txt file containing "hi" in /workspace.
image: prehnite-base:latest
network: false                # legacy shorthand; equivalent to {mode: none}
timeout_seconds: 60
setup:
  - mkdir -p /workspace
verify:
  - test -f /workspace/hello.txt
  - grep -q hi /workspace/hello.txt
```

`network` accepts three shapes:

- `network: false` (or omitted) — `{mode: none}`, no networking at all.
- `network: true` — `{mode: full}`, bridged with no proxy or logging.
- `network: {mode: restricted, extra_allow: [...]}` — bridged through an
  HTTP CONNECT proxy that enforces a hardcoded allowlist plus
  `extra_allow`. Every connection attempt (allowed or denied) becomes an
  `egress_attempt` event in the trajectory.

Optional `tags: [str]` and `difficulty: str` fields enable filtering via
`list_tasks(tag, difficulty)`.

## Trajectory format

One JSON object per line. Event types in v0:

| `type`           | Fields                                                                |
| ---------------- | --------------------------------------------------------------------- |
| `run_started`    | `task_id`, `image`, `container_id`, `network` (NetworkSpec dump)      |
| `setup_command`  | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`                 |
| `agent_command`  | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`                 |
| `agent_thought`  | `thought` (free-form reasoning the agent recorded)                    |
| `egress_attempt` | `host`, `port`, `allowed`, `reason`, `duration_ms` (restricted mode)  |
| `verify_command` | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`                 |
| `run_finished`   | `result` (`passed`/`failed`/`error`), `reason`                        |

All events also carry `ts` (UTC ISO-8601) and `seq` (monotonic 0-indexed int).

**Temporal note for trajectory readers:** `egress_attempt` events are written
by the proxy thread while a command's `exec_run` is still in flight, so they
land at slightly lower `seq` numbers than the `agent_command` whose execution
triggered them. That ordering is temporally correct — the network call
happens *inside* the exec — and `TrajectoryWriter` is `Lock`-guarded so the
proxy thread and main thread can't race or produce torn lines.

## Project layout

See [CLAUDE.md](CLAUDE.md). The interesting modules are under `src/prehnite/`.

## Development

```bash
uv sync --extra dev
uv run pytest                  # unit + integration; integration skipped if no Docker
uv run pytest -m "not integration"
uv run mypy src/prehnite
```

## License

MIT
