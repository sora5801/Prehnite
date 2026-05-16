"""Stdio MCP server that exposes Prehnite tasks to an agent.

The agent drives a task interactively:

    list_tasks(tag?, difficulty?)  -> [{id, description, tags, difficulty}, ...]
    describe_task(task_id)         -> full Task spec as a dict
    start_task(task_id)            -> {session_id, container_id, trajectory_path}
    exec(session_id, cmd)          -> CommandResult
    note(session_id, thought)      -> None                 (records reasoning)
    read_trajectory(session_id,    -> list[event]          (reflect on own run so far)
                    since_seq?)
    fork(session_id)               -> {snapshot_id}        (snapshot for revert)
    revert(session_id,             -> {container_id}       (roll back to snapshot)
           snapshot_id)
    finish_task(session_id)        -> RunResult            (runs verify, tears down)
    abort_task(session_id)         -> None                 (tears down without verify)

Sessions survive an MCP server restart. Each `start_task` writes a small
JSON descriptor at <root>/sessions/<session_id>.json that records the
container id and trajectory path. On `build_server()` startup the
descriptors are read back and the in-process sessions dict is rehydrated
by re-attaching to the still-running detached containers. `restricted`
network-mode sessions can't be resumed (the agent's container has
HTTP_PROXY pointing at a port the previous process owned); those are
dropped at rehydrate time.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from prehnite.runner import overflow_dir, trajectory_path
from prehnite.sandbox import Sandbox, SandboxError
from prehnite.schemas import RunResult, RunStatus, Task, TrajectoryEvent
from prehnite.tasks.loader import discover_tasks
from prehnite.trajectory import TrajectoryWriter
from prehnite.trajectory import read_trajectory as _read_trajectory_file


@dataclass
class _Session:
    task: Task
    sandbox: Sandbox
    writer: TrajectoryWriter
    trajectory_path: Path
    agent_command_count: int = 0


def _root() -> Path:
    return Path(os.environ.get("PREHNITE_ROOT", Path.cwd())).resolve()


def _tasks_dir() -> Path:
    env = os.environ.get("PREHNITE_TASKS_DIR")
    return Path(env).resolve() if env else _root() / "tasks"


def _sessions_dir(root: Path) -> Path:
    return root / "sessions"


def _session_file_path(root: Path, session_id: str) -> Path:
    return _sessions_dir(root) / f"{session_id}.json"


def _persist_session(root: Path, session_id: str, sess: "_Session") -> None:
    """Write the session descriptor to disk so a future MCP server process
    can resume this session after a restart. Called once on start_task."""
    _sessions_dir(root).mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "task": sess.task.model_dump(mode="json"),
        "container_id": sess.sandbox.container_id,
        "trajectory_path": str(sess.trajectory_path),
        "started_at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "network_mode": sess.task.network.mode,
    }
    path = _session_file_path(root, session_id)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _delete_session_file(root: Path, session_id: str) -> None:
    """Remove the session descriptor on finish_task / abort_task. Best-effort;
    errors are swallowed because the in-memory teardown already happened."""
    try:
        _session_file_path(root, session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _rehydrate_sessions(root: Path) -> dict[str, "_Session"]:
    """Scan <root>/sessions/ on startup. For each descriptor:
    - skip + delete the file if it's restricted-mode (proxy port is lost);
    - skip + delete the file if the container is gone;
    - otherwise re-attach to the container, reopen the trajectory writer
      (which recovers the seq counter), recover agent_command_count from
      the trajectory, and add an entry to the resumed sessions dict.
    """
    out: dict[str, _Session] = {}
    sdir = _sessions_dir(root)
    if not sdir.is_dir():
        return out

    for f in sorted(sdir.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"prehnite-mcp: skipping malformed session file {f}: {e}", file=sys.stderr)
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        sid = str(payload.get("session_id", ""))
        if not sid:
            f.unlink(missing_ok=True)
            continue

        if payload.get("network_mode") == "restricted":
            print(
                f"prehnite-mcp: cannot resume restricted-mode session {sid} "
                f"(proxy port from prior process is gone); dropping",
                file=sys.stderr,
            )
            f.unlink(missing_ok=True)
            continue

        try:
            task = Task.model_validate(payload["task"])
        except Exception as e:
            print(f"prehnite-mcp: session {sid} has bad task payload: {e}", file=sys.stderr)
            f.unlink(missing_ok=True)
            continue

        traj_path = Path(payload["trajectory_path"])
        sandbox = Sandbox(task)
        try:
            sandbox.attach(str(payload["container_id"]))
        except SandboxError as e:
            print(
                f"prehnite-mcp: session {sid} container is gone ({e}); dropping",
                file=sys.stderr,
            )
            f.unlink(missing_ok=True)
            continue

        writer = TrajectoryWriter(traj_path, overflow_dir=overflow_dir(root))
        writer.open()  # recovers the seq counter from the existing file

        out[sid] = _Session(
            task=task,
            sandbox=sandbox,
            writer=writer,
            trajectory_path=traj_path,
            agent_command_count=_count_agent_commands(traj_path),
        )
        print(
            f"prehnite-mcp: resumed session {sid} (task={task.id}, "
            f"container={payload['container_id'][:12]})",
            file=sys.stderr,
        )

    return out


def _count_agent_commands(trajectory_path: Path) -> int:
    """Recover agent_command_count by counting matching events in the
    trajectory. The trajectory is the source of truth for what happened,
    so we don't need to persist the counter separately."""
    try:
        events = _read_trajectory_file(trajectory_path)
    except Exception:
        return 0
    return sum(1 for e in events if e.type == "agent_command")


def _filter_branch(
    events: "list[TrajectoryEvent]", branch: str
) -> "list[TrajectoryEvent]":
    """Filter trajectory events by fork/revert branch.

    `branch` values:
    - "all": return everything (no filter)
    - "current": exclude events strictly between a session_forked and
      its matching session_reverted — i.e., the events on a branch that
      was rolled back. session_forked and session_reverted events
      themselves are part of the current timeline (they record what
      really happened).
    - "<snapshot_id>": return events on the branch starting at that
      fork. If the snapshot was reverted, this is the events between
      the fork and the revert. If the fork exists but no revert
      matches, returns everything after the fork (it's the "live"
      branch starting at that snapshot).

    Unknown snapshot_id returns []. Empty event list returns [] for any
    branch.
    """
    if branch == "all" or not events:
        return list(events)

    # Map snapshot_id -> the seq of its session_forked event.
    fork_seq: dict[str, int] = {}
    for e in events:
        if e.type == "session_forked":
            snap_id = str(e.data.get("snapshot_id", ""))
            if snap_id:
                fork_seq[snap_id] = e.seq

    # For each revert that references a known fork, record the discarded
    # range (fork_seq, revert_seq) — events strictly inside were rolled
    # back. If the same snap_id is reverted multiple times, the latest
    # revert wins (the earlier discarded events remain discarded by the
    # subsequent revert's range).
    discarded: dict[str, tuple[int, int]] = {}
    for e in events:
        if e.type == "session_reverted":
            snap_id = str(e.data.get("snapshot_id", ""))
            if snap_id in fork_seq:
                discarded[snap_id] = (fork_seq[snap_id], e.seq)

    if branch == "current":
        discarded_seqs: set[int] = set()
        for f_seq, r_seq in discarded.values():
            discarded_seqs.update(range(f_seq + 1, r_seq))
        return [e for e in events if e.seq not in discarded_seqs]

    # branch == specific snapshot_id
    if branch in discarded:
        f_seq, r_seq = discarded[branch]
        return [e for e in events if f_seq < e.seq < r_seq]
    if branch in fork_seq:
        # Fork exists but no matching revert — return events after the fork.
        f_seq = fork_seq[branch]
        return [e for e in events if e.seq > f_seq]
    return []  # unknown snapshot id


def build_server(
    sessions: dict[str, _Session] | None = None,
) -> FastMCP:
    """Construct a FastMCP server.

    `sessions` is exposed so tests can inject fake sessions without going
    through start_task (which needs Docker). In production it's `None`,
    and the dict is rehydrated from `<root>/sessions/*.json` so an MCP
    server restart picks up where the previous process left off.
    """
    server = FastMCP("prehnite")
    if sessions is None:
        sessions = _rehydrate_sessions(_root())

    @server.tool()
    def list_tasks(
        tag: str | None = None,
        difficulty: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks under the configured tasks directory.

        Optional filters: pass `tag` to keep only tasks whose `tags` list
        contains it; pass `difficulty` to keep only tasks whose `difficulty`
        equals it. Both are AND-combined.
        """
        out: list[dict[str, Any]] = []
        for t in discover_tasks(_tasks_dir()):
            if tag is not None and tag not in t.tags:
                continue
            if difficulty is not None and t.difficulty != difficulty:
                continue
            out.append(
                {
                    "id": t.id,
                    "description": t.description,
                    "tags": list(t.tags),
                    "difficulty": t.difficulty,
                }
            )
        return out

    @server.tool()
    def describe_task(task_id: str) -> dict[str, Any]:
        """Return the full task spec for `task_id` (everything in the YAML
        file). Useful when an agent wants the verify steps, setup commands,
        or other context that `list_tasks` doesn't include."""
        return _find_task(task_id).model_dump(mode="json")

    @server.tool()
    def start_task(task_id: str) -> dict[str, Any]:
        """Start a fresh sandbox for `task_id` and run its setup commands.

        Returns the session id you'll pass to `exec` / `finish_task`, the
        container id, and the path the trajectory is being written to.
        """
        task = _find_task(task_id)
        out_path = trajectory_path(task, _root())
        writer = TrajectoryWriter(out_path, overflow_dir=overflow_dir(_root()))
        writer.open()

        def _record_egress(data: dict[str, object]) -> None:
            writer.write("egress_attempt", data)

        sandbox = Sandbox(task, egress_callback=_record_egress)
        try:
            sandbox.start()
        except SandboxError as e:
            writer.write(
                "run_finished",
                {"result": RunStatus.ERROR.value, "reason": f"sandbox start: {e}"},
            )
            writer.close()
            raise

        writer.write(
            "run_started",
            {
                "task_id": task.id,
                "image": task.image,
                "container_id": sandbox.container_id,
                "network": task.network.model_dump(mode="json"),
            },
        )

        # Run setup before handing the session to the agent.
        for cmd in task.setup:
            result = sandbox.exec(cmd)
            writer.write("setup_command", result.model_dump())
            if result.exit_code != 0:
                reason = f"setup command failed (exit {result.exit_code}): {cmd}"
                writer.write(
                    "run_finished",
                    {"result": RunStatus.ERROR.value, "reason": reason},
                )
                writer.close()
                sandbox.stop()
                raise RuntimeError(reason)

        sid = uuid.uuid4().hex
        sess = _Session(
            task=task, sandbox=sandbox, writer=writer, trajectory_path=out_path,
        )
        sessions[sid] = sess
        # Persist after setup succeeds — a session file means "live session,
        # safe to resume." If setup failed we wouldn't be here.
        _persist_session(_root(), sid, sess)
        return {
            "session_id": sid,
            "container_id": sandbox.container_id,
            "trajectory_path": str(out_path),
        }

    @server.tool()
    def exec(session_id: str, cmd: str) -> dict[str, Any]:
        """Run a single shell command inside the session's sandbox.

        Large `stdout`/`stderr` is capped (default 8 KiB per stream).
        Truncated streams are marked with `<field>_truncated: true` and
        the full output is spilled to `<root>/overflow/<sha256>` —
        accessible to a human reviewer with `cat`, and stable across
        sessions thanks to content-addressing.
        """
        sess = _require(sessions, session_id)
        result = sess.sandbox.exec(cmd)
        event = sess.writer.write("agent_command", result.model_dump())
        sess.agent_command_count += 1
        # Return the post-truncation data so the agent's response is
        # bounded — matching what they'd read back via read_trajectory.
        return dict(event.data)

    @server.tool()
    def note(session_id: str, thought: str) -> None:
        """Record your reasoning here between commands — what you tried, why
        you tried it, what you expect to happen. The more reasoning you
        record, the more useful the trajectory is."""
        sess = _require(sessions, session_id)
        sess.writer.write("agent_thought", {"thought": thought})

    @server.tool()
    def fork(session_id: str) -> dict[str, Any]:
        """Snapshot the current container state. Returns a snapshot_id you
        can pass to revert() to roll back to this exact state.

        Use this before a risky action (an `rm`, a destructive refactor,
        an experimental approach you're not sure about). If it doesn't
        work out, call revert(session_id, snapshot_id) and try
        something else — no need to start the task over.

        A snapshot is ~1–5s to take (docker commit on the container),
        so pick snapshot points deliberately rather than snapshotting
        after every command. Snapshots are cleaned up when the session
        ends (finish_task / abort_task), so they don't outlive the run.
        """
        sess = _require(sessions, session_id)
        snap_id = sess.sandbox.snapshot(
            extra_labels={"prehnite.session_id": session_id}
        )
        sess.writer.write(
            "session_forked",
            {
                "snapshot_id": snap_id,
                "container_id": sess.sandbox.container_id,
            },
        )
        return {"snapshot_id": snap_id}

    @server.tool()
    def revert(session_id: str, snapshot_id: str) -> dict[str, Any]:
        """Replace the current container with a fresh one created from the
        snapshot. Returns the new container_id. Any work done since the
        snapshot was taken is discarded.

        The session_id stays the same across reverts — exec / note /
        read_trajectory all keep working. The trajectory records a
        session_reverted event with both the snapshot_id and the new
        container_id, so a future reader sees the discontinuity
        explicitly.
        """
        sess = _require(sessions, session_id)
        previous_cid = sess.sandbox.container_id
        new_cid = sess.sandbox.revert(snapshot_id)
        sess.writer.write(
            "session_reverted",
            {
                "snapshot_id": snapshot_id,
                "previous_container_id": previous_cid,
                "new_container_id": new_cid,
            },
        )
        # The session descriptor pins the container_id used for session
        # persistence (devlog 0017). After revert it's stale — rewrite it
        # so a future MCP restart attaches to the new container.
        _persist_session(_root(), session_id, sess)
        return {"container_id": new_cid}

    @server.tool()
    def read_trajectory(
        session_id: str,
        since_seq: int = 0,
        branch: str = "all",
    ) -> list[dict[str, Any]]:
        """Read back your own trajectory so far — every event recorded in
        this session, including setup commands, your own exec/note calls,
        and any egress attempts. Useful when you need to recall what you
        tried, what failed, what stdout you saw, or what you noted
        earlier. Each event has `seq`, `ts`, `type`, and `data` keys.

        Set `since_seq` to fetch only events with seq >= since_seq — e.g.,
        if you last read up to seq=10, pass since_seq=11 to get just
        what's happened since. Default 0 returns everything.

        Set `branch` to filter by fork/revert history:
        - "all" (default): every event in the trajectory.
        - "current": exclude events strictly between a session_forked
          and its matching session_reverted (the path that was rolled
          back). Useful when you've done multiple reverts and want to
          focus on the live timeline.
        - "<snapshot_id>": only events on that snapshot's branch —
          from its fork up to its revert (or to end if not yet
          reverted). Useful when you want to recall "what did I try
          on the branch I abandoned?"

        This is a read; it doesn't count as agent activity (the
        agent_command_count that distinguishes "no activity" from
        "verify failed" is untouched)."""
        sess = _require(sessions, session_id)
        events = _read_trajectory_file(sess.trajectory_path)
        events = _filter_branch(events, branch)
        return [
            e.model_dump(mode="json") for e in events if e.seq >= since_seq
        ]

    @server.tool()
    def finish_task(session_id: str) -> dict[str, Any]:
        """Run verify, write the final event, tear down the sandbox."""
        sess = sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(f"unknown session_id: {session_id}")

        try:
            verify_failures: list[str] = []
            for cmd in sess.task.verify:
                result = sess.sandbox.exec(cmd)
                sess.writer.write("verify_command", result.model_dump())
                if result.exit_code != 0:
                    verify_failures.append(cmd)

            status = RunStatus.PASSED if not verify_failures else RunStatus.FAILED
            if status is RunStatus.PASSED:
                reason = "all verify checks passed"
            elif sess.agent_command_count == 0:
                reason = "no agent activity (verify ran on untouched workspace)"
            else:
                reason = f"verify failed: {verify_failures}"
            sess.writer.write(
                "run_finished", {"result": status.value, "reason": reason}
            )
            return RunResult(
                task_id=sess.task.id,
                status=status,
                reason=reason,
                trajectory_path=sess.trajectory_path,
                container_id=sess.sandbox.container_id,
            ).model_dump(mode="json")
        finally:
            sess.sandbox.stop()
            sess.writer.close()
            _delete_session_file(_root(), session_id)

    @server.tool()
    def abort_task(session_id: str) -> dict[str, str]:
        """Tear down a session without running verify. Use when the agent gives up."""
        sess = sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(f"unknown session_id: {session_id}")
        sess.writer.write(
            "run_finished",
            {"result": RunStatus.ERROR.value, "reason": "aborted by agent"},
        )
        sess.sandbox.stop()
        sess.writer.close()
        _delete_session_file(_root(), session_id)
        return {"session_id": session_id, "status": "aborted"}

    return server


def _find_task(task_id: str) -> Task:
    for task in discover_tasks(_tasks_dir()):
        if task.id == task_id:
            return task
    raise KeyError(f"unknown task_id: {task_id}")


def _require(sessions: dict[str, _Session], session_id: str) -> _Session:
    sess = sessions.get(session_id)
    if sess is None:
        raise KeyError(f"unknown session_id: {session_id}")
    return sess


def main() -> None:
    """Console-script entry point: serve over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
