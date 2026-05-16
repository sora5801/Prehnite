from __future__ import annotations

from pathlib import Path

import pytest

from prehnite.tasks.loader import TaskLoadError, discover_tasks, load_task


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_load_task_minimal(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "id: hello\ndescription: hi\n")
    t = load_task(p)
    assert t.id == "hello"
    assert t.description == "hi"


def test_load_task_full(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "t.yaml",
        """
id: full
description: a fully populated task
image: my-image:1
network: true
timeout_seconds: 60
workdir: /work
setup:
  - echo setup
verify:
  - test -f /work/done
""".lstrip(),
    )
    t = load_task(p)
    assert t.network.mode == "full"  # legacy `network: true` -> mode=full
    assert t.image == "my-image:1"
    assert t.workdir == "/work"
    assert t.setup == ["echo setup"]
    assert t.verify == ["test -f /work/done"]


def test_load_task_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TaskLoadError, match="not found"):
        load_task(tmp_path / "nope.yaml")


def test_load_task_invalid_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "id: [unterminated\n")
    with pytest.raises(TaskLoadError, match="invalid YAML"):
        load_task(p)


def test_load_task_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "- a\n- b\n")
    with pytest.raises(TaskLoadError, match="mapping"):
        load_task(p)


def test_load_task_schema_failure(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "id: NotLowercase\ndescription: x\n")
    with pytest.raises(TaskLoadError, match="schema validation"):
        load_task(p)


def test_discover_tasks_sorted_and_dedup(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", "id: alpha\ndescription: a\n")
    _write(tmp_path / "sub" / "b.yml", "id: beta\ndescription: b\n")
    tasks = discover_tasks(tmp_path)
    assert sorted(t.id for t in tasks) == ["alpha", "beta"]


def test_discover_tasks_duplicate_id(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", "id: dup\ndescription: a\n")
    _write(tmp_path / "b.yaml", "id: dup\ndescription: b\n")
    with pytest.raises(TaskLoadError, match="duplicate"):
        discover_tasks(tmp_path)


def test_discover_tasks_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(TaskLoadError, match="not found"):
        discover_tasks(tmp_path / "nope")


def test_bundled_examples_load() -> None:
    """The example tasks shipped in the repo must always parse."""
    repo_root = Path(__file__).resolve().parent.parent
    examples = repo_root / "tasks" / "examples"
    tasks = discover_tasks(examples)
    ids = {t.id for t in tasks}
    assert {"hello", "fix_off_by_one"}.issubset(ids)
