"""YAML task spec loader.

Tasks live as `*.yaml` / `*.yml` files in a directory. The loader parses each
file into a `Task` model — pydantic does the validation, so a malformed task
fails loud at load time, not at run time.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from prehnite.schemas import Task


class TaskLoadError(Exception):
    """A task file is missing, unparseable, or fails schema validation."""


def load_task(path: Path) -> Task:
    if not path.is_file():
        raise TaskLoadError(f"task file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TaskLoadError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise TaskLoadError(f"{path}: expected a YAML mapping at the top level")
    try:
        return Task.model_validate(raw)
    except ValidationError as e:
        raise TaskLoadError(f"{path}: schema validation failed:\n{e}") from e


def discover_tasks(root: Path) -> list[Task]:
    """Load every `*.yaml`/`*.yml` under `root`, deduplicated by id."""
    if not root.is_dir():
        raise TaskLoadError(f"task directory not found: {root}")

    tasks: dict[str, Task] = {}
    for path in sorted(_yaml_files(root)):
        task = load_task(path)
        if task.id in tasks:
            raise TaskLoadError(
                f"duplicate task id {task.id!r}: defined in both "
                f"{tasks[task.id]} and {path}"
            )
        tasks[task.id] = task
    return list(tasks.values())


def _yaml_files(root: Path) -> Iterable[Path]:
    yield from root.rglob("*.yaml")
    yield from root.rglob("*.yml")
