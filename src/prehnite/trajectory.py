"""Append-only JSONL trajectory writer.

CLAUDE.md invariant: trajectory writes are append-only. Once a writer is closed
the file is sealed; nothing in this module ever mutates a previously-written
line.

Each event is one JSON object on its own line, terminated by `\\n`. We flush
after every write so a killed process leaves a usable partial trajectory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from prehnite.schemas import EventType, TrajectoryEvent, utcnow_iso


class TrajectoryWriter:
    """Sequenced, append-only writer for a single trajectory file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None
        self._seq = 0
        self._closed = False

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
        # Append mode: if a writer with this path was used before, we keep the
        # earlier events. Sequence numbers count only what *this* writer added,
        # which is fine because the file already has its own contiguous range.
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, event_type: EventType, data: dict[str, object]) -> TrajectoryEvent:
        if self._closed:
            raise RuntimeError("trajectory writer is closed")
        if self._fh is None:
            raise RuntimeError("trajectory writer is not open")

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
