# 0005 — Three new task shapes

**Date:** 2026-05-15
**Status:** ✅ shipped, all five examples now in the suite

## What

Three new YAML tasks under [`tasks/examples/`](../../tasks/examples/) that
exercise dimensions the original two (`hello`, `fix_off_by_one`) didn't
touch:

| Task | Shape it tests |
| --- | --- |
| [merge_configs.yaml](../../tasks/examples/merge_configs.yaml) | multi-file investigation (3 files: defaults.json, overrides.json, merge.py) |
| [install_cowsay.yaml](../../tasks/examples/install_cowsay.yaml) | `network: true` opt-in — agent must `pip install` from PyPI |
| [fix_log_stats.yaml](../../tasks/examples/fix_log_stats.yaml) | two compounding bugs — fixing the obvious one isn't enough |

## merge_configs

Setup writes `defaults.json` (`{"db": {"host": "localhost", "port": 5432}, ...}`),
`overrides.json` (`{"db": {"host": "prod.example.com", "ssl": true}}`), and a
`merge.py` that does a *shallow* merge with `{**defaults, **overrides}`. The
shallow merge clobbers `defaults.db.port` because `overrides.db` replaces
the whole `db` dict.

Verify expects the deep-merged stringified JSON. The agent has to:

- read all three files to understand the data flow,
- recognise that `{**a, **b}` is the wrong primitive,
- replace it with a recursive merge.

Headless smoke (no fix → fails; with deep-merge replacement → passes) both
behave correctly.

## install_cowsay

This is the first task in the suite that uses `network: true`. The MCP
sandbox switches `network_mode` from `none` to `bridge`, so the container
gets the default Docker bridge with NAT to the host's network and can reach
PyPI.

Setup pre-creates a venv at `/workspace/venv` (so the task isn't testing
`python -m venv`, which is local) but doesn't install `cowsay`. Verify just
does

```
/workspace/venv/bin/python /workspace/app.py | grep -q "hello prehnite"
```

If `cowsay` isn't installed, `app.py` raises `ImportError` and prints
nothing to stdout. `grep -q` on empty input exits 1 → verify fails. So the
verify test implicitly checks both the install AND the runtime — no
separate `import` check needed.

**End-to-end agent run** ([trajectory](../../trajectories/install_cowsay/20260516T034045Z.jsonl)):
the agent ran `pip install cowsay` and got `exit=0`, then ran `app.py` and
got `exit=0`, then `finish_task` ran the grep and it passed. The pip
install reaching PyPI is the stronger signal — `exit=0` from pip means
network was reachable, which means `network: true` translated all the way
through to a working bridged network at the daemon level.

If `network: true` had been ignored or misconfigured (e.g.,
`network_mode=none` had stuck), pip would have failed with a
`ConnectionError` and the task would have errored out. It didn't.

## fix_log_stats

Two bugs in `stats.py`, each subtle on its own:

1. **Regex last octet** uses `\d` instead of `\d+`, so any IP whose last
   component is more than one digit silently fails to match. The data set
   includes one `192.168.1.10` that's invisible to the parser.
2. **Off-by-one** at `print(len(fivexx_ips) - 1)` — the `-1` looks
   intentional (maybe the original author was discounting a header line)
   but isn't.

Either bug alone doesn't produce the expected `3`. The agent has to fix
both. The design intent: even if the agent fixes only the more visible
bug first (off-by-one), running the script returns the wrong number, the
agent reads the code more carefully, finds the regex bug, fixes that too.

In practice, **Sonnet 4.6 caught both bugs in one read** of the source
([trajectory](../../trajectories/fix_log_stats/20260516T034128Z.jsonl) is
just `cat → sed → cat → run → finish`). That's fine — the task isn't
*required* to be multi-shot, just *prone to* multi-shot. Verified the
multi-shot path independently: a headless run that fixes only the
off-by-one (`sed 's/print(len(fivexx_ips) - 1)/print(len(fivexx_ips))/'`)
correctly reports `verify failed`, which is exactly the signal an
iterating agent needs.

For training-data purposes, the multi-shot variant might emerge naturally
from weaker agents. The task design is what matters; the per-run shape
follows from the agent.

## Gotcha: YAML and `:` in shell verify lines

`merge_configs.yaml` initially had its verify command as a plain block
sequence item:

```yaml
verify:
  - cd /workspace && test "$(python3 merge.py)" = '{"cache": {"ttl": 60}, ...}'
```

The unquoted `:` characters inside the JSON literal made YAML think it was
parsing a nested mapping. Loader correctly raised `TaskLoadError: invalid
YAML: ... expected <block end>, but found ','`. Wrapped the whole verify
command in a literal block scalar (`|`) and it parses cleanly.

This is the kind of failure mode that's easy to write by accident when
the test data happens to be JSON. Worth a future touch on `loader.py`:
the current error message is forwarded straight from PyYAML, which is
accurate but not friendly. A wrapper that says "did you mean to wrap the
command in `|`?" would save someone time.

## Verification

| What | Result |
| --- | --- |
| `discover_tasks` finds all 5 | ✓ — 5 ids printed, including `network=True` |
| Pytest full suite | ✓ — 35/35 still pass |
| `merge_configs` headless: no fix | failed with "no agent activity" |
| `merge_configs` headless: deep-merge fix | passed |
| `install_cowsay` headless: no fix | failed with "no agent activity" |
| `install_cowsay` headless: `pip install cowsay` | **passed (network reached PyPI)** |
| `fix_log_stats` headless: no fix | failed with "no agent activity" |
| `fix_log_stats` headless: only off-by-one fixed | failed with "verify failed" (multi-shot path proven) |
| `fix_log_stats` headless: both bugs fixed | passed |
| `install_cowsay` via MCP agent (Sonnet) | passed |
| `fix_log_stats` via MCP agent (Sonnet) | passed (one-shot) |

## Diff size

```
docs/devlog/0005-three-task-shapes.md      | 132 +++++++++++++++++++++++
tasks/examples/fix_log_stats.yaml          |  47 ++++++++
tasks/examples/install_cowsay.yaml         |  31 ++++++
tasks/examples/merge_configs.yaml          |  47 ++++++++
4 files changed, 257 insertions(+)
```

Pure additions. No production code touched.
