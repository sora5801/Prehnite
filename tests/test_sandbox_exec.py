"""Unit tests for the Sandbox.exec output-unpacking path.

The docker SDK's `Container.exec_run(..., demux=True)` returns
`ExecResult(exit_code, output)` where `output` is a `(stdout_bytes,
stderr_bytes)` tuple — but the type stubs union `output` across all argument
shapes as `int | bytes | None`. sandbox.py bridges that with a `cast` and a
tuple unpack. These tests fake the container so they run without Docker, and
exercise:

- the four shapes `output` can take with demux on
  (both streams / stdout only / stderr only / no output)
- the `exit_code` propagation
- the `demux=True` contract — the cast is only correct as long as exec_run
  is called with `demux=True`. A future change that flips it would silently
  break stdout/stderr capture; this test catches that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prehnite.sandbox import Sandbox
from prehnite.schemas import Task


@dataclass
class _FakeExecResult:
    exit_code: int
    output: Any


class _FakeContainer:
    def __init__(self, result: _FakeExecResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def exec_run(self, **kwargs: Any) -> _FakeExecResult:
        self.calls.append(kwargs)
        return self._result


def _make(output: Any, exit_code: int = 0) -> tuple[Sandbox, _FakeContainer]:
    sb = Sandbox(Task(id="t", description="x"))
    fake = _FakeContainer(_FakeExecResult(exit_code=exit_code, output=output))
    sb._container = fake  # type: ignore[assignment]
    return sb, fake


def test_exec_unpacks_both_streams() -> None:
    sb, _ = _make(output=(b"hi\n", b"err\n"))
    r = sb.exec("anything")
    assert r.exit_code == 0
    assert r.stdout == "hi\n"
    assert r.stderr == "err\n"


def test_exec_handles_only_stdout() -> None:
    sb, _ = _make(output=(b"hi\n", None))
    r = sb.exec("anything")
    assert r.stdout == "hi\n"
    assert r.stderr == ""


def test_exec_handles_only_stderr() -> None:
    sb, _ = _make(output=(None, b"oops\n"), exit_code=2)
    r = sb.exec("anything")
    assert r.exit_code == 2
    assert r.stdout == ""
    assert r.stderr == "oops\n"


def test_exec_handles_no_output() -> None:
    sb, _ = _make(output=None)
    r = sb.exec("anything")
    assert r.stdout == ""
    assert r.stderr == ""


def test_exec_demands_demux_true() -> None:
    """Lock in the contract that makes the cast valid."""
    sb, fake = _make(output=(None, None))
    sb.exec("anything")
    assert fake.calls[0].get("demux") is True


# --- per-exec timeout ----------------------------------------------------


def test_exec_wraps_cmd_with_timeout_using_task_default() -> None:
    """The cmd Docker actually runs is `timeout N sh -c <cmd>`; N comes from
    the Task's exec_timeout_seconds default when no kwarg is given."""
    sb, fake = _make(output=(None, None))
    sb.exec("echo hello")
    actual = fake.calls[0]["cmd"]
    assert actual == ["timeout", "60", "sh", "-c", "echo hello"]


def test_exec_timeout_kwarg_overrides_task_default() -> None:
    sb, fake = _make(output=(None, None))
    sb.exec("echo hello", timeout_seconds=3)
    assert fake.calls[0]["cmd"] == ["timeout", "3", "sh", "-c", "echo hello"]


def test_exec_timeout_kwarg_accepts_fractional_seconds() -> None:
    sb, fake = _make(output=(None, None))
    sb.exec("echo hello", timeout_seconds=0.5)
    assert fake.calls[0]["cmd"] == ["timeout", "0.5", "sh", "-c", "echo hello"]


def test_exec_records_cmd_as_user_wrote_it_not_the_wrapped_form() -> None:
    """Trajectory consumers should see the agent's actual command, not the
    proxy/runtime wrapper. exec returns a CommandResult; its cmd field is
    what gets serialised into the trajectory."""
    sb, _ = _make(output=(b"hi\n", None))
    r = sb.exec("echo hello")
    assert r.cmd == "echo hello"
