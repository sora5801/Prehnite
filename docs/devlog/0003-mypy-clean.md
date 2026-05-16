# 0003 — mypy is now clean

**Date:** 2026-05-15
**Status:** ✅ shipped

## What

`uv run mypy src/prehnite` now exits 0 — was 2 errors. Three small changes:

- Added `types-docker>=7.1` to dev deps in [pyproject.toml](../../pyproject.toml).
- Removed `docker.*` from the `ignore_missing_imports` mypy override.
- Added one `cast` in [sandbox.py](../../src/prehnite/sandbox.py) for an
  imprecision in the docker stubs.

## Why

The two errors were pre-existing since v0:

```
src/prehnite/sandbox.py:39: Name "docker.DockerClient" is not defined  [name-defined]
src/prehnite/sandbox.py:62: Module has no attribute "from_env"         [attr-defined]
```

The docker SDK ships without `py.typed`, so `import docker; docker.X` left
mypy with no clue what `X` was. The original override
(`ignore_missing_imports = true` for `docker.*`) suppressed import-not-found
errors but did nothing for attribute access — those still leaked through.

`types-docker` is the canonical fix. Anthropic-grade Python projects don't
swallow attribute-error diagnostics with `# type: ignore` when an upstream
stubs package exists; the stubs catch real bugs.

## The new error the stubs surfaced (and why a `cast` is right)

Once the stubs were active, two new errors appeared at the `_decode` calls:

```python
stdout_b, stderr_b = exec_result.output or (None, None)
# error: Argument 1 to "_decode" has incompatible type "int | bytes | None";
# expected "bytes | None"
```

The stubs declare `ExecResult.output` as `int | bytes | None`, which is the
union across all `exec_run` argument shapes. With `demux=True`, the runtime
always returns a `(stdout_bytes, stderr_bytes)` tuple — but the stubs don't
narrow on `demux`. (To do that they'd need overloads keyed on the literal
type of `demux`, which the upstream stubs don't ship.)

Three options I considered:

1. **`# type: ignore[arg-type]` × 2.** Cheapest, most opaque. A future
   reader has to figure out *why* the type is being ignored.
2. **Restructure to `isinstance` check.** Honest at the type level but adds
   five lines of branching to handle a case (`output` is an `int`) that
   never happens with our `demux=True` call.
3. **`cast`.** One line. Names the runtime invariant we're relying on
   (`tuple[bytes | None, bytes | None] | None`) so a stub fix in the
   upstream package would be a quick search-and-delete here.

Picked (3). The cast site has a one-line comment explaining the invariant
so a future reader knows the cast is documenting reality, not papering over
a bug.

## Verification

- `uv run mypy src/prehnite` → `Success: no issues found in 9 source files`.
- `uv run pytest` → 30 passed (unchanged from previous commit).

## Diff size

```
pyproject.toml          | 4 ++--
src/prehnite/sandbox.py | 7 +++++--
2 files changed, 7 insertions(+), 4 deletions(-)
```

Three lines of real code change, plus one dev-dep and one mypy-config tweak.
