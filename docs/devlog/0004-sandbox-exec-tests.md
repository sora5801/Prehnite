# 0004 — Lock in the demux=True cast with unit tests

**Date:** 2026-05-15
**Status:** ✅ shipped

## What

[`tests/test_sandbox_exec.py`](../../tests/test_sandbox_exec.py) — five
fast, Docker-less unit tests that fake `Container.exec_run` and exercise
the `Sandbox.exec` output-unpacking path that
[devlog 0003](0003-mypy-clean.md) added a `cast` for.

## Regressions each test catches

| Test | Catches |
| --- | --- |
| `test_exec_unpacks_both_streams` | swapping stdout/stderr; misreading the tuple order |
| `test_exec_handles_only_stdout` | stripping `b""`-vs-`None` distinction in `_decode`; assuming both elements are present |
| `test_exec_handles_only_stderr` | exit_code propagation regressing; same as above |
| `test_exec_handles_no_output` | dropping the `or (None, None)` fallback (would `TypeError` on unpack) |
| `test_exec_demands_demux_true` | flipping `demux=True` off, which silently invalidates the cast and would break stdout/stderr capture |

The last one is the interesting one — it tests a *contract*, not behavior.
The cast in [sandbox.py](../../src/prehnite/sandbox.py) is only correct as
long as `exec_run` is called with `demux=True`. Without that test, a future
refactor that flips `demux` to satisfy some other concern would type-check
clean and pass every other test, then truncate or interleave streams in
prod. With that test, the change fails CI immediately and the author has
to confront the cast.

## Why not extend test_sandbox.py

[`tests/test_sandbox.py`](../../tests/test_sandbox.py) is integration-only
(`pytestmark = [pytest.mark.integration, pytest.mark.skipif(no Docker)]`)
and already covers the happy path against a real container. These new
tests cover edge cases (None output, missing streams) that are hard to
trigger with real shell commands, and they need to run on machines
without Docker — the natural split is a separate file.

## Diff size

```
docs/devlog/0004-sandbox-exec-tests.md | 47 +++++++++++++++++++++++++
tests/test_sandbox_exec.py             | 87 ++++++++++++++++++++++++++++++++++
2 files changed, 134 insertions(+)
```

Pure test addition. No production code touched.
