"""Append-only JSONL trajectory writer.

CLAUDE.md invariant: trajectory writes are append-only. Once a writer is closed
the file is sealed; nothing in this module ever mutates a previously-written
line.

Each event is one JSON object on its own line, terminated by `\\n`. We flush
after every write so a killed process leaves a usable partial trajectory.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from prehnite.overflow import DEFAULT_MAX_BYTES, cap_command_data
from prehnite.schemas import EventType, TrajectoryEvent, utcnow_iso

# Event types whose `data` dicts carry command output worth bounding.
# `stdout`/`stderr` on these can come straight from a build tool or a
# `cat large_file`; truncating here is what keeps trajectories small and
# the agent's context window healthy when it calls `read_trajectory`.
_CAPPED_EVENT_TYPES: frozenset[str] = frozenset(
    {"setup_command", "agent_command", "verify_command"}
)


class TrajectoryWriter:
    """Sequenced, append-only writer for a single trajectory file."""

    def __init__(
        self,
        path: Path,
        *,
        overflow_dir: Path | None = None,
        max_stream_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = path
        self._fh: IO[str] | None = None
        self._seq = 0
        self._closed = False
        # Egress proxy writes from a background thread; serialize seq + write.
        self._lock = threading.Lock()
        # If overflow_dir is set, command-event stdout/stderr get capped
        # at `max_stream_bytes` and the original is spilled there via
        # prehnite.overflow.cap_command_data. None disables truncation
        # entirely — handy for tests that want raw output.
        self._overflow_dir = overflow_dir
        self._max_stream_bytes = max_stream_bytes

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # If we're reopening an existing trajectory (e.g. a session resume
        # after the MCP server restarted), recover the seq counter from the
        # last event's seq+1 so new events continue the numbering instead of
        # restarting at 0 and stomping on the existing sequence.
        if self.path.exists() and self.path.stat().st_size > 0:
            self._seq = _recover_next_seq(self.path)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, event_type: EventType, data: dict[str, object]) -> TrajectoryEvent:
        with self._lock:
            if self._closed:
                raise RuntimeError("trajectory writer is closed")
            if self._fh is None:
                raise RuntimeError("trajectory writer is not open")

            if (
                self._overflow_dir is not None
                and event_type in _CAPPED_EVENT_TYPES
            ):
                data = cap_command_data(
                    data,
                    max_bytes=self._max_stream_bytes,
                    overflow_dir=self._overflow_dir,
                )

            event = TrajectoryEvent(
                seq=self._seq,
                ts=utcnow_iso(),
                type=event_type,
                data=data,
            )
            self._fh.write(event.model_dump_json() + "\n")
            self._fh.flush()
            self._seq += 1
            return event

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._closed = True


def read_trajectory(path: Path) -> list[TrajectoryEvent]:
    """Parse a trajectory file back into events. Used by tests and tooling."""
    events: list[TrajectoryEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(TrajectoryEvent.model_validate(json.loads(line)))
    return events


def _recover_next_seq(path: Path) -> int:
    """Walk the file and return last event's `seq` + 1. Used by the writer's
    open() to support session-resume after an MCP server restart. Cheap:
    parses only the last non-empty line as JSON, not the whole file."""
    last_line = ""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped
    if not last_line:
        return 0
    try:
        obj = json.loads(last_line)
        return int(obj.get("seq", -1)) + 1
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0
