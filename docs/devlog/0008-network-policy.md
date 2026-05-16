# 0008 — Network policy: three-tier mode, egress proxy, trajectory events

**Date:** 2026-05-15
**Status:** ✅ shipped, end-to-end verified

## What

The "Decisions made → Network policy" section that CLAUDE.md added —
three-tier `none / restricted / full` with allowlist + egress proxy +
non-negotiable trajectory logging — is now real code. A real agent run of
`install_cowsay` through `restricted` mode produced this:

```
seq=0  run_started  network={'mode': 'restricted', 'extra_allow': []}
seq=5  EGRESS  pypi.org:443                allowed=True  (22ms)  matched allowlist
seq=6  EGRESS  files.pythonhosted.org:443  allowed=True  (32ms)  matched allowlist
seq=7  agent_command  exit=0  /workspace/venv/bin/pip install cowsay
```

Two PyPI hosts were dialled, both matched the default allowlist (the
second via suffix match on `pythonhosted.org`), and pip installed cowsay
cleanly. The trajectory has the full audit log a future training pass
needs to filter on "did the agent talk to anything off-policy."

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  prehnite-mcp process (host)                                         │
│                                                                      │
│   TrajectoryWriter  ◄────── EgressProxy (per-session, random port)   │
│        ▲                         ▲                                   │
│        │                         │                                   │
│        └── exec/setup/verify     │ HTTP CONNECT                      │
│                writes            │                                   │
│                                  │                                   │
└──────────────────────────────────│───────────────────────────────────┘
                                   │
   ┌───────────────────────────────│──────────────────────────────┐
   │  Sandbox container (Docker bridge)                           │
   │                                                              │
   │   HTTP_PROXY=http://host.docker.internal:<port>              │
   │                                                              │
   │   pip → tries to reach pypi.org → routed through proxy ──────┘
   │                                                              │
   └──────────────────────────────────────────────────────────────┘
```

- The proxy lives **in the host process**, not in a sidecar container.
  No extra Docker image to build. The container reaches it via
  `host.docker.internal`, which Docker Desktop provides natively and
  the Sandbox maps explicitly via `extra_hosts={"host.docker.internal":
  "host-gateway"}` for Linux Docker compatibility.
- One proxy per `start_task` invocation. Allowlist is
  `DEFAULT_ALLOWLIST ∪ spec.extra_allow`. Port is OS-assigned (random
  free) so multiple sessions can coexist.
- The proxy handles **HTTP CONNECT only** — that's the method pip,
  GitHub, and every modern HTTPS client uses. Other methods get
  `405 Method Not Allowed`. We get the destination host:port, not the
  URL path. Deeper inspection (mitmproxy with CA injection) is a
  separate decision; v0's scope is "log what you can without rewriting
  TLS."
- Domains are **suffix-matched**: `pythonhosted.org` in the allowlist
  also matches `files.pythonhosted.org`. This is what makes the
  default allowlist short and useful.

## Why the proxy lives in the host process

Three options were on the table:

| Approach | Verdict |
| --- | --- |
| **Tiny Python relay in the host process** | ✅ Picked. Calls the trajectory writer directly. No image to build. One Python module. |
| mitmproxy sidecar container | Heavyweight, needs a CA injection scheme, adds a Docker image to maintain. Defer until HTTPS path-level visibility is actually needed. |
| iptables + dnsmasq, no proxy | Loses host:port visibility (only DNS + connection attempts). Platform-fragile across Docker Desktop variants. |

The in-process proxy fits the "shell-level + selective enrichment"
philosophy we've been building: keep the moving parts inside Python
where we control them, push observability into the trajectory.

## Trajectory ordering — egress events precede the command that caused them

In the verification run, the agent's `exec("pip install cowsay")` call
ends up at `seq=7` while the egress events are at `seq=5,6`. That's
correct, not a bug:

1. Agent's `exec` tool call starts: docker `exec_run` begins.
2. Inside the container, pip opens a TCP connection → CONNECT to the
   proxy → the proxy's handler thread writes `egress_attempt` event
   (gets the lower seq).
3. pip finishes. `exec_run` returns. Main thread writes `agent_command`
   event (gets the higher seq).

The trajectory's `seq` is global monotonic write order across all
threads, which is the temporally-correct ordering even when the events
share an `exec`. The `TrajectoryWriter.write()` method serializes both
threads on a lock so we never get torn writes or duplicate seq numbers.

This was the one new concurrency point: `TrajectoryWriter` got a
`threading.Lock` around the `seq` increment + file write. Without it,
the proxy thread and the main thread could both read the same `_seq`
and produce duplicates.

## Backward compatibility

CLAUDE.md picked `network: true → mode=full`, so every existing task in
the repo (and any external YAML out there) keeps working without
migration. The legacy bool is coerced by a `field_validator` on the
`network` field:

```python
@field_validator("network", mode="before")
@classmethod
def _coerce_legacy_bool(cls, v: Any) -> Any:
    if v is True: return {"mode": "full"}
    if v is False: return {"mode": "none"}
    return v
```

Only `install_cowsay.yaml` was migrated — to `mode: restricted` —
because that's the task that actually demonstrates the new code path.
The other four are still on legacy `false`/`true` shorthand.

## DEFAULT_ALLOWLIST

Conservative, deliberately small:

```
pypi.org, pythonhosted.org, github.com, githubusercontent.com,
registry.npmjs.org, crates.io, static.crates.io, httpbin.org, example.com
```

Covers Python/Node/Rust package managers and the standard test
endpoints. Anything else needs to come in via per-task `extra_allow` —
that way the global default is auditable in one place and each task
opts in explicitly to anything weird.

## What was deliberately not done

- **HTTPS path-level visibility.** We see `pypi.org:443` but not
  `/simple/cowsay/`. Adding that means mitmproxy + CA injection in
  `prehnite-base` + agents trusting an arbitrary CA. Not worth it
  unless a training/eval need shows up.
- **Egress events for `full` mode.** The proxy doesn't exist in
  `full` — that mode is "raw bridge, you're on your own." If full
  mode needs auditing later, restricted with a permissive allowlist
  is the answer, not adding the proxy back into full.
- **DNS-level logging.** The proxy sees outbound TCP connections.
  DNS lookups happen via Docker's resolver and aren't captured. If
  a future task does a DNS lookup but never connects, we won't log
  it. Acceptable for v0; revisit when there's a concrete reason.
- **Process / file IO logging.** Out of scope. Trajectory captures
  shell-level commands and now egress; deeper instrumentation is a
  whole separate axis.

## Tests

- [`tests/test_schemas.py`](../../tests/test_schemas.py) — five new
  tests for `NetworkSpec` defaults, legacy `true`/`false` coercion,
  explicit-dict round-trip, and `egress_attempt` event round-trip.
- [`tests/test_egress_proxy.py`](../../tests/test_egress_proxy.py) —
  new file. Allowlist matching (exact, suffix, partial-substring
  non-match, empty set), plus three end-to-end tests using a loopback
  echo server to verify the proxy forwards bytes on allowed
  destinations, returns 403 on denied destinations, and rejects
  non-CONNECT methods. No Docker involvement.
- Existing tests updated where they checked `network is True/False`
  (now check `network.mode`). Loader, schemas, and MCP-describe tests
  all carry the change.

## Verification

- `uv run pytest` → 56 passed (44 prior + 5 schemas + 7 egress proxy).
- `uv run mypy src/prehnite` → clean.
- Real Sonnet 4.6 agent driving `install_cowsay` through `restricted`
  mode → `passed`. Trajectory:
  [trajectories/install_cowsay/20260516T045556Z.jsonl](../../trajectories/install_cowsay/20260516T045556Z.jsonl).
  Two `egress_attempt` events (both `allowed=True`), agent dropped two
  `agent_thought` events alongside two `agent_command` calls, verify
  passed.

## Diff size

```
docs/devlog/0008-network-policy.md  | ~180 +
src/prehnite/egress_proxy.py        | ~220 +
src/prehnite/sandbox.py             |   48 +
src/prehnite/schemas.py             |   33 +
src/prehnite/runner.py              |    7 +
src/prehnite/mcp_server.py          |    9 +
src/prehnite/trajectory.py          |    7 +
tasks/examples/install_cowsay.yaml  |    6 +
README.md                           |   14 +
tests/test_egress_proxy.py          | ~120 +
tests/test_schemas.py               |   46 +
tests/test_loader.py                |    2 +
tests/test_mcp_server.py            |    2 +
tests/test_runner.py                |    4 +
```

Biggest single commit on the project so far. End-to-end vertical slice
through every layer: schema, sandbox, proxy, runner, MCP server, tests,
docs, example task migration, and a real-agent run that exercises the
whole stack.
