# 0009 — `egress_allowlist` example task

**Date:** 2026-05-15
**Status:** ✅ shipped

## Why

[`install_cowsay`](../../tasks/examples/install_cowsay.yaml) demonstrated
the `restricted` mode happy path — every connection the agent issued
matched the allowlist, so every `egress_attempt` event was
`allowed=true`. The deny path (proxy refuses, agent gets `403 from proxy
after CONNECT`, trajectory records `allowed=false`) wasn't exercised by
any example task. A future reader skimming `tasks/examples/` couldn't
see a denial without contriving one.

[`egress_allowlist.yaml`](../../tasks/examples/egress_allowlist.yaml)
fills that gap: one probe each at an allowed host (`example.com`, in
the default allowlist) and a denied host (`www.iana.org`, not in it).
The verify suite checks that curl reports `200` for the allowed probe
and a non-zero exit for the denied probe.

## What the agent's trajectory looks like

From a real Sonnet 4.6 run via MCP
([trajectories/egress_allowlist/20260516T061720Z.jsonl](../../trajectories/egress_allowlist/20260516T061720Z.jsonl)):

```
seq=0  run_started      network={mode: restricted, extra_allow: []}
seq=2  THOUGHT          Plan: curl example.com with -w "%{http_code}" to capture status...
seq=3  EGRESS           example.com:443     allowed=True   (32ms)  matched allowlist
seq=4  agent_command    curl ... https://example.com > /workspace/ok_status      exit=0
seq=5  agent_command    cat /workspace/ok_status                                  exit=0  stdout: 200
seq=6  THOUGHT          example.com returned 200 as expected — proxy allowed the CONNECT.
                        Now attempting www.iana.org which is NOT in the allowlist...
seq=7  EGRESS           www.iana.org:443    allowed=False  (0ms)   not in allowlist
seq=8  agent_command    curl https://www.iana.org; echo $? > /workspace/blocked_exitcode
seq=9  agent_command    cat /workspace/blocked_exitcode                           exit=0  stdout: 56
seq=10 THOUGHT          Both files are in place and contain the expected values...
seq=11–14 verify_command (×4, all exit=0)
seq=15 run_finished     passed
```

Two details worth pointing at:

1. **Duration delta makes the deny visible.** The allowed event ran
   for 32ms because the proxy actually opened a TCP connection to
   `93.184.216.34:443` and waited for TLS to complete before logging
   `allowed=true`. The denied event ran for 0ms because the proxy
   returned `403` before any DNS lookup or socket connect — exactly
   the security property `restricted` mode is meant to provide.
2. **curl exit 56 (`CURLE_RECV_ERROR`)** is what curl returns when an
   HTTP proxy returns a non-2xx response to its `CONNECT` request.
   That's the agent-visible signal. The trajectory's `egress_attempt`
   event is the operator-visible signal. They corroborate each other
   without coupling.

## Design choices

- **Default allowlist + no `extra_allow`.** Wanted the task to fail in
  a way that's purely about the default policy, not a per-task allow
  hole. So `extra_allow: []` and the test relies on `example.com`
  being in `DEFAULT_ALLOWLIST` (which it is, intended as a test fixture
  exactly for this purpose).
- **`www.iana.org` as the denied target, not a synthetic
  `denied.example`.** A real domain proves the deny isn't dependent
  on DNS failure — the proxy refuses before resolution even runs.
  IANA's site is stable and nobody's likely to add it to the default
  allowlist by accident (different concern from package mirrors).
- **Verify doesn't read the trajectory.** Verify runs inside the
  sandbox container, but the trajectory lives on the host. So the
  contract is: verify checks observable in-container effects (curl
  exit code and HTTP status), and the trajectory tells the audit
  story separately. Both must agree for the task to be useful, but
  neither needs to peek at the other.

## Verification

- `prehnite run egress_allowlist --cmd '<probes>'` (headless) — passed,
  trajectory has both `egress_attempt` events with the expected
  shape.
- Sonnet 4.6 driving via MCP — passed, with three `agent_thought`
  events annotating the plan / mid-task observation / final check, and
  separate `exec` calls per probe (instead of the headless batched
  command). The natural agent rhythm produced a cleaner trajectory
  than my headless --cmd run.

## Diff size

```
docs/devlog/0009-egress-allowlist-example.md | 67 +
tasks/examples/egress_allowlist.yaml         | 33 +
```

Pure addition. No production code touched. Task corpus is now 6
examples, covering the smoke / single-file fix / multi-file fix /
network-allowed / network-denied / multi-shot dimensions.
