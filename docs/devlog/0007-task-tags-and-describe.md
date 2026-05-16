# 0007 — Task tags, difficulty, and `describe_task`

**Date:** 2026-05-15
**Status:** ✅ shipped (Track 1 of an audit-driven sweep)

## Why

The user's CLAUDE.md added a "Decisions made → MCP tool surface" section
that described two refinements not yet in the code:

> - `list_tasks` supports filtering by tag and difficulty.
> - `describe_task` returns the full task spec including instructions, so
>   agents can get per-tool-like context when needed.

This devlog covers the additive pieces (additions only — no breaking
changes to the existing tools or task spec). The bigger network-policy
refactor that section also lists is being planned separately and shipped
in its own commit (Track 2).

## What changed

- [`schemas.py`](../../src/prehnite/schemas.py): two new optional fields
  on `Task` — `tags: list[str]` (default `[]`) and `difficulty: str | None`
  (default `None`). Both keep the existing `frozen=True, extra="forbid"`
  semantics.
- [`mcp_server.py`](../../src/prehnite/mcp_server.py):
  - `list_tasks(tag, difficulty)` accepts both filters (AND-combined,
    both optional). Returns `id`, `description`, `tags`, `difficulty`
    per task.
  - New `describe_task(task_id)` tool — returns the full Task spec via
    `model_dump(mode="json")`.
- All five [`tasks/examples/*.yaml`](../../tasks/examples/) annotated with
  reasonable `tags` + `difficulty`. Picked a small starter taxonomy —
  `smoke`, `bug-fix`, `python`, `multi-file`, `network`,
  `package-management`, `regex` for tags; `trivial`, `easy`, `medium` for
  difficulty. Easy to retune; the MCP filter doesn't care about the values.
- README: updated MCP tool inventory; one-line note that `tags` and
  `difficulty` are optional and enable filtering.
- CLAUDE.md cleanups (separate from the git commit since CLAUDE.md is
  untracked):
  - `Dependency management: [FILL IN — uv, poetry, or pip-tools]` → `uv`.
  - `[PROJECT_NAME]/` placeholders in the layout block → `prehnite/`.
  - The "Decisions made → MCP tool surface" section now accurately
    describes the shipped session-shaped tools (was listing
    `run_task / list_tasks / describe_task` which contradicted the v0
    code).
  - Removed the `[FILL IN — replace with your actual current uncertainties.
    Examples:]` template line above the now-real "Open questions" entry.

## One small surprise: dict vs list returns through FastMCP

While writing tests I hit a wrinkle: `server.call_tool(...)` returns
`(content_blocks, raw_result)`. The shape of `raw_result` depends on what
the tool returns:

- Tool returns `list[...]`: `raw_result = {"result": [...]}`. The list is
  wrapped under a `result` key.
- Tool returns `dict[...]`: `raw_result = {...}`. The dict is passed
  through unwrapped — its own keys ARE the structured content.
- Tool returns scalar (e.g. `int`): `raw_result = {"result": 6}`.

This is FastMCP's behaviour: dicts already look like structured content,
so it doesn't add a wrapper. Tests for `list_tasks` use `raw["result"]`;
the test for `describe_task` uses `raw` directly. Noted with a one-line
comment in [`tests/test_mcp_server.py`](../../tests/test_mcp_server.py)
so the next reader doesn't get caught by the same thing.

## Tests

[`tests/test_mcp_server.py`](../../tests/test_mcp_server.py) gains a
`fake_tasks_dir` pytest fixture that writes three deliberately-varied
mini-tasks (`alpha`, `beta`, `gamma`) into a `tmp_path` and points
`PREHNITE_TASKS_DIR` at it. Six new tests on top:

- `list_tasks` with no filter returns all
- `list_tasks(tag="bug-fix")` keeps only `beta` and `gamma`
- `list_tasks(difficulty="medium")` keeps only `gamma`
- `list_tasks(tag="bug-fix", difficulty="easy")` AND-combines → `beta`
- `describe_task("beta")` returns the full spec including model defaults
  (network, workdir) plus YAML-only fields (tags, difficulty)
- `describe_task` on an unknown id raises

## Verification

- `uv run pytest` → 44 passed (was 38; +1 schemas miss-correction
  carry-over, +5 MCP-server tests covering filtering and describe_task)
- `uv run mypy src/prehnite` → clean

## Diff size

```
docs/devlog/0007-task-tags-and-describe.md |  ~120 +
README.md                                  |    8 +-
src/prehnite/mcp_server.py                 |   30 ++-
src/prehnite/schemas.py                    |    3 +
tasks/examples/fix_log_stats.yaml          |    2 +
tasks/examples/fix_off_by_one.yaml         |    2 +
tasks/examples/hello.yaml                  |    2 +
tasks/examples/install_cowsay.yaml         |    2 +
tasks/examples/merge_configs.yaml          |    2 +
tests/test_mcp_server.py                   |  ~85 +
```

`runner.py`, `cli.py`, `sandbox.py`, `trajectory.py`, `tasks/loader.py`:
untouched. Network policy is the next track.
