"""Tests for the bounded-stream overflow helpers.

The trajectory writer uses these to cap stdout/stderr on command events
and spill the original to a content-addressed file. Tests poke the
helpers directly so any change in behavior (boundary handling, dedupe,
metadata shape) is caught without a full writer + sandbox round-trip.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from prehnite.overflow import (
    DEFAULT_MAX_BYTES,
    cap_command_data,
    cap_stream,
)


def test_small_text_passes_through_unchanged(tmp_path: Path) -> None:
    text = "small output"
    out, meta = cap_stream(text, max_bytes=DEFAULT_MAX_BYTES, overflow_dir=tmp_path)
    assert out == text
    assert meta == {}
    # No file should be written for non-overflow input.
    assert list(tmp_path.iterdir()) == []


def test_large_text_is_truncated_and_spilled(tmp_path: Path) -> None:
    text = "x" * 10_000
    out, meta = cap_stream(text, max_bytes=1024, overflow_dir=tmp_path)
    assert len(out.encode("utf-8")) <= 1024
    assert meta["truncated"] is True
    assert meta["original_bytes"] == 10_000
    sha = meta["overflow_sha256"]
    assert isinstance(sha, str) and len(sha) == 64

    spilled = tmp_path / sha
    assert spilled.is_file()
    assert spilled.read_bytes() == text.encode("utf-8")


def test_identical_overflow_dedupes(tmp_path: Path) -> None:
    """Two calls with the same payload write one file (content-addressed)."""
    text = "y" * 5_000
    _, meta1 = cap_stream(text, max_bytes=512, overflow_dir=tmp_path)
    _, meta2 = cap_stream(text, max_bytes=512, overflow_dir=tmp_path)
    assert meta1["overflow_sha256"] == meta2["overflow_sha256"]
    # Only one file in the overflow dir.
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == meta1["overflow_sha256"]


def test_sha256_matches_full_original(tmp_path: Path) -> None:
    text = "z" * 9_000
    _, meta = cap_stream(text, max_bytes=2048, overflow_dir=tmp_path)
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert meta["overflow_sha256"] == expected


def test_utf8_boundary_is_respected(tmp_path: Path) -> None:
    """Truncation point lands on a valid UTF-8 boundary so the returned
    head always round-trips through .decode()."""
    # "あ" is 3 bytes in UTF-8 (E3 81 82). max_bytes=4 would land us
    # mid-multibyte for the second character — the helper should walk
    # back to the boundary at byte 3 (end of first char).
    text = "あ" * 4  # 12 bytes
    out, meta = cap_stream(text, max_bytes=4, overflow_dir=tmp_path)
    # The head decodes cleanly — no replacement chars or mojibake.
    assert "�" not in out
    # And it's a prefix of the original.
    assert text.startswith(out)
    assert meta["truncated"] is True
    assert meta["original_bytes"] == 12


def test_cap_command_data_caps_both_streams(tmp_path: Path) -> None:
    data: dict[str, object] = {
        "cmd": "noisy",
        "exit_code": 0,
        "stdout": "a" * 5_000,
        "stderr": "b" * 5_000,
        "duration_ms": 1,
    }
    capped = cap_command_data(data, max_bytes=1024, overflow_dir=tmp_path)
    assert capped["cmd"] == "noisy"  # untouched
    assert capped["exit_code"] == 0  # untouched
    assert capped["stdout_truncated"] is True
    assert capped["stderr_truncated"] is True
    assert capped["stdout_original_bytes"] == 5_000
    assert capped["stderr_original_bytes"] == 5_000
    # Two distinct overflow files (one per stream — content differs).
    assert capped["stdout_overflow_sha256"] != capped["stderr_overflow_sha256"]
    assert len(list(tmp_path.iterdir())) == 2


def test_cap_command_data_leaves_small_streams_alone(tmp_path: Path) -> None:
    data: dict[str, object] = {
        "cmd": "quiet",
        "exit_code": 0,
        "stdout": "ok\n",
        "stderr": "",
        "duration_ms": 1,
    }
    capped = cap_command_data(data, max_bytes=DEFAULT_MAX_BYTES, overflow_dir=tmp_path)
    assert capped == data
    # No file written.
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_cap_command_data_does_not_mutate_input(tmp_path: Path) -> None:
    data: dict[str, object] = {
        "cmd": "loud",
        "exit_code": 0,
        "stdout": "x" * 10_000,
        "stderr": "",
        "duration_ms": 1,
    }
    snapshot = dict(data)
    cap_command_data(data, max_bytes=512, overflow_dir=tmp_path)
    assert data == snapshot
