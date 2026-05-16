# Prehnite

Sandboxed Linux environment with a task runner and trajectory logger, aimed at
developers fine-tuning or evaluating coding agents.

In one sentence: **agents run inside an isolated Docker container, perform coding
tasks, and every action they take is captured as structured JSONL we can replay
or train on.**

## Status

v0 — the end-to-end spine works: an agent connects via MCP, picks a task, runs
shell commands inside an ephemeral Docker container, and a trajectory is written
to disk.

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

# Run an example task end-to-end without MCP (useful for smoke-testing)
uv run prehnite run tasks/examples/hello.yaml

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

Wire it into a client (Claude Desktop, Claude Code, or any MCP client) by
pointing it at:

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
network: false
timeout_seconds: 60
setup:
  - mkdir -p /workspace
verify:
  - test -f /workspace/hello.txt
  - grep -q hi /workspace/hello.txt
```

Optional `tags: [str]` and `difficulty: str` fields enable filtering via
`list_tasks(tag, difficulty)`.

## Trajectory format

One JSON object per line. Event types in v0:

| `type`           | Fields                                                    |
| ---------------- | --------------------------------------------------------- |
| `run_started`    | `task_id`, `image`, `container_id`, `network`             |
| `setup_command`  | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`     |
| `agent_command`  | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`     |
| `agent_thought`  | `thought` (free-form reasoning the agent recorded)        |
| `verify_command` | `cmd`, `exit_code`, `stdout`, `stderr`, `duration_ms`     |
| `run_finished`   | `result` (`passed`/`failed`/`error`), `reason`            |

All events also carry `ts` (UTC ISO-8601) and `seq` (monotonic 0-indexed int).

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
