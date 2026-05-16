"""Tests for `prehnite reap` — host cleanup of orphan containers + stale logs.

Docker is mocked via a tiny fake client + fake containers; no real daemon
is contacted. Session JSON descriptors are written by hand (same shape
the MCP server's _persist_session would produce) so the cross-reference
between candidate containers and live sessions is exercised end-to-end.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from prehnite import cli
from prehnite.cli import main


# --- fakes --------------------------------------------------------------


class _FakeImage:
    """Stand-in for a docker.models.images.Image. Also used as a
    Container's `image` attribute (which only needs `.tags` access)."""

    def __init__(
        self,
        tags: list[str],
        image_id: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.id = image_id
        self.tags = tags
        self.attrs = {"Config": {"Labels": labels or {}}}


class _FakeContainer:
    """Stand-in for a docker.models.containers.Container.

    Records remove(force=True) calls so tests can assert it was reaped."""

    def __init__(
        self,
        cid: str,
        image_tags: list[str],
        labels: dict[str, str] | None = None,
        status: str = "exited",
        exit_code: int | None = 0,
    ) -> None:
        self.id = cid
        self.image = _FakeImage(image_tags)
        self.labels = labels or {}
        self.status = status
        self.attrs = {"State": {"ExitCode": exit_code}}
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainerCollection:
    """Implements .list(all=True, filters={...}) over a fixed set."""

    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers

    def list(self, all: bool = False, filters: dict[str, Any] | None = None):
        filters = filters or {}
        out: list[_FakeContainer] = []
        for c in self._containers:
            if "label" in filters:
                want = filters["label"]
                # docker-py accepts label as "k=v" or just "k"
                key, _, val = want.partition("=")
                got = c.labels.get(key)
                if val and got != val:
                    continue
                if not val and key not in c.labels:
                    continue
            if "ancestor" in filters:
                want = filters["ancestor"]
                if not any(t.startswith(want) for t in c.image.tags):
                    continue
            out.append(c)
        return out


class _FakeImageCollection:
    def __init__(self, images: list[_FakeImage]) -> None:
        self._images = images
        self.removed: list[str] = []

    def list(self, filters: dict[str, Any] | None = None):
        filters = filters or {}
        out: list[_FakeImage] = []
        for img in self._images:
            if "label" in filters:
                want = filters["label"]
                key, _, val = want.partition("=")
                got = (img.attrs.get("Config", {}).get("Labels") or {}).get(key)
                if val and got != val:
                    continue
                if not val and got is None:
                    continue
            out.append(img)
        return out

    def remove(self, image_id: str, force: bool = False) -> None:
        self.removed.append(image_id)


class _FakeDockerClient:
    def __init__(
        self,
        containers: list[_FakeContainer],
        images: list[_FakeImage] | None = None,
    ) -> None:
        self.containers = _FakeContainerCollection(containers)
        self.images = _FakeImageCollection(images or [])


def _patch_docker(
    monkeypatch: pytest.MonkeyPatch,
    containers: list[_FakeContainer],
    images: list[_FakeImage] | None = None,
) -> None:
    """Replace docker.from_env() with a fake that returns the given list."""
    import docker

    monkeypatch.setattr(
        docker, "from_env", lambda: _FakeDockerClient(containers, images)
    )


def _write_session_json(root: Path, sid: str, container_id: str) -> None:
    """Mirror what mcp_server._persist_session writes — minimal subset."""
    sdir = root / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "task": {"id": "x", "description": "x"},
        "container_id": container_id,
        "trajectory_path": str(root / "trajectories" / "x" / f"{sid}.jsonl"),
        "started_at_iso": "2026-05-16T00:00:00Z",
        "network_mode": "none",
    }
    (sdir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


# --- container reaping --------------------------------------------------


def test_reap_removes_orphan_labeled_container(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = _FakeContainer(
        "deadbeefdeadbeefdeadbeef",
        ["prehnite-base:latest"],
        labels={"prehnite": "true", "prehnite.task_id": "hello"},
    )
    _patch_docker(monkeypatch, [orphan])

    rc = main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert orphan.removed is True
    assert "deadbeefdead" in out  # short id in the plan
    assert "task=hello" in out
    assert "Reaped 1 containers" in out
    assert rc == 0


def test_reap_keeps_container_that_is_in_a_live_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container whose ID appears in <root>/sessions/*.json must NOT be
    reaped — it's actively in use by an MCP session."""
    live = _FakeContainer(
        "live1234567890abcdef0000",
        ["prehnite-base:latest"],
        labels={"prehnite": "true", "prehnite.task_id": "demo"},
        status="running",
        exit_code=None,
    )
    _patch_docker(monkeypatch, [live])
    _write_session_json(tmp_path, "abc", live.id)

    rc = main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert live.removed is False
    assert "nothing to reap" in out
    assert rc == 0


def test_reap_mixes_live_and_orphan_correctly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _FakeContainer("live000000000000", ["prehnite-base:latest"], labels={"prehnite": "true"})
    orphan = _FakeContainer("dead000000000000", ["prehnite-base:latest"], labels={"prehnite": "true"})
    _patch_docker(monkeypatch, [live, orphan])
    _write_session_json(tmp_path, "sid", live.id)

    main(["reap", "--root", str(tmp_path)])

    assert live.removed is False
    assert orphan.removed is True


def test_reap_picks_up_pre_label_containers_by_image(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container from before we started labeling — only the image matches.
    The ancestor-filter sweep should still catch it."""
    legacy = _FakeContainer(
        "legacy0000000000",
        ["prehnite-base:latest"],
        labels={},  # no prehnite label
    )
    _patch_docker(monkeypatch, [legacy])

    main(["reap", "--root", str(tmp_path)])

    assert legacy.removed is True


def test_reap_ignores_unrelated_containers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containers from other images, without prehnite labels, must be
    untouched."""
    unrelated = _FakeContainer("other00000000000", ["redis:7-alpine"], labels={})
    _patch_docker(monkeypatch, [unrelated])

    main(["reap", "--root", str(tmp_path)])

    assert unrelated.removed is False


# --- dry-run ------------------------------------------------------------


def test_reap_dry_run_does_not_remove(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = _FakeContainer("dead00000000", ["prehnite-base:latest"], labels={"prehnite": "true"})
    _patch_docker(monkeypatch, [orphan])

    rc = main(["reap", "--root", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out

    assert orphan.removed is False
    assert "dry-run" in out
    assert "dead00000000" in out
    assert rc == 0


# --- batch-logs cleanup -------------------------------------------------


def test_reap_deletes_stale_batch_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_docker(monkeypatch, [])  # no containers
    logs = tmp_path / "batch-logs"
    logs.mkdir()
    stale = logs / "old.log"
    fresh = logs / "new.log"
    stale.write_text("old\n", encoding="utf-8")
    fresh.write_text("new\n", encoding="utf-8")
    # Make stale 48h old; fresh stays "now"
    old_mtime = time.time() - 48 * 3600
    os.utime(stale, (old_mtime, old_mtime))

    rc = main(["reap", "--root", str(tmp_path), "--older-than-hours", "24"])
    out = capsys.readouterr().out

    assert not stale.exists()
    assert fresh.exists()
    assert "Reaped 0 containers, 0 snapshots, and 1 batch logs" in out
    assert rc == 0


def test_reap_dry_run_keeps_stale_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_docker(monkeypatch, [])
    logs = tmp_path / "batch-logs"
    logs.mkdir()
    stale = logs / "old.log"
    stale.write_text("x\n", encoding="utf-8")
    os.utime(stale, (time.time() - 48 * 3600,) * 2)

    main(["reap", "--root", str(tmp_path), "--dry-run"])

    assert stale.exists()


# --- edge cases ---------------------------------------------------------


def test_reap_no_orphans_no_logs_prints_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_docker(monkeypatch, [])
    rc = main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "nothing to reap" in out
    assert rc == 0


def test_reap_handles_missing_sessions_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sessions/ dir means no live sessions — every prehnite container is
    an orphan. Don't crash trying to scan a non-existent dir."""
    orphan = _FakeContainer("dead000000", ["prehnite-base:latest"], labels={"prehnite": "true"})
    _patch_docker(monkeypatch, [orphan])
    # tmp_path has no sessions/ subdir.
    main(["reap", "--root", str(tmp_path)])
    assert orphan.removed is True


def test_reap_docker_unavailable_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If docker daemon is unreachable, exit 2 with a clear error."""
    import docker
    from docker.errors import DockerException

    def _raise() -> None:
        raise DockerException("daemon unreachable")

    monkeypatch.setattr(docker, "from_env", _raise)

    rc = main(["reap", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "could not reach Docker daemon" in err


# --- snapshot image orphan cleanup --------------------------------------


def test_reap_removes_orphan_snapshot_with_no_live_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot image tagged with prehnite.session_id pointing at a
    session that no longer exists is an orphan — reap it."""
    orphan_snap = _FakeImage(
        tags=["prehnite-snapshot:dead123"],
        image_id="sha256:imgdead",
        labels={"prehnite.snapshot": "true", "prehnite.session_id": "gone-sid"},
    )
    _patch_docker(monkeypatch, [], images=[orphan_snap])
    # No sessions/ dir at all means gone-sid is definitely orphan.

    rc = main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert "Snapshot images to reap" in out
    assert "prehnite-snapshot:dead123" in out
    assert "Reaped 0 containers, 1 snapshots, and 0 batch logs" in out
    assert rc == 0


def test_reap_keeps_snapshot_belonging_to_live_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot whose session is still live (descriptor exists) is kept."""
    live_snap = _FakeImage(
        tags=["prehnite-snapshot:livesnap"],
        image_id="sha256:imglive",
        labels={"prehnite.snapshot": "true", "prehnite.session_id": "live-sid"},
    )
    _patch_docker(monkeypatch, [], images=[live_snap])
    _write_session_json(tmp_path, "live-sid", container_id="live-container-x")

    main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert "nothing to reap" in out


def test_reap_treats_unlabeled_snapshot_as_orphan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot image without prehnite.session_id (manual `docker commit`
    or pre-label-era) can't be matched to any session — treat as orphan."""
    untagged = _FakeImage(
        tags=["prehnite-snapshot:mystery"],
        image_id="sha256:mystery",
        labels={"prehnite.snapshot": "true"},  # no session_id
    )
    _patch_docker(monkeypatch, [], images=[untagged])

    main(["reap", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert "Reaped 0 containers, 1 snapshots" in out
