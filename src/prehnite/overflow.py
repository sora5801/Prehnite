"""Bounded text storage for trajectory event payloads.

Command stdout/stderr can grow to megabytes (build logs, full file dumps,
verbose test output). If we drop those straight into a `TrajectoryEvent`
they bloat the JSONL file, fight the MCP message-size limits when an
agent calls `read_trajectory`, and on `exec()` return paths can poison
the agent's context window with a single rogue command.

The fix is head-only truncation with content-addressed overflow:

1. If the text fits under `max_bytes` (UTF-8 length), pass it through
   untouched. No file is written.
2. Otherwise, write the FULL original bytes to
   `<overflow_dir>/<sha256>` and return only the first `max_bytes`
   along with metadata pointing at the file:

       {
           "stdout": "<truncated head>",
           "stdout_truncated": True,
           "stdout_overflow_sha256": "<hex>",
           "stdout_original_bytes": <int>,
       }

The full output is never lost — a reviewer can `cat overflow/<sha>` to
recover it. The agent gets enough head to see what the command was
doing without flooding its context.

Head-only (not head+tail) was the deliberate choice: most commands put
the actionable signal up top (error messages, stack traces, the first
failing assertion). Tail can be useful for test runners that summarize
at the end, but those summaries are usually small enough to fit
naturally; if not, the agent can still fetch the full output from
overflow.

Content-addressing means identical outputs dedupe automatically — a
loop that prints the same 100KB blob 50 times writes one overflow file,
not 50.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 8 * 1024
"""Per-stream cap. 8 KiB fits a couple of pages of output and is small
enough that an agent can call exec() dozens of times without filling its
context window. Tunable per writer."""

# Field pairs we apply truncation to. Each is `(field_name, metadata_prefix)`.
_CAPPED_FIELDS: tuple[tuple[str, str], ...] = (
    ("stdout", "stdout"),
    ("stderr", "stderr"),
)


def cap_stream(
    text: str,
    *,
    max_bytes: int,
    overflow_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Truncate `text` to `max_bytes` of UTF-8, spilling overflow to disk.

    Returns `(possibly_truncated_text, metadata_dict)`. The metadata is
    empty when no truncation happened, or `{truncated: True,
    overflow_sha256, original_bytes}` when it did.

    `max_bytes` is interpreted on the UTF-8-encoded form so we don't
    blow past a byte budget just because the text contains multibyte
    characters. The truncation point is then trimmed back to the nearest
    valid UTF-8 boundary so the head we return is always decodable.
    """
    if not isinstance(text, str):
        return text, {}  # let non-string values pass through unchanged

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, {}

    # Hash the full original so overflow files dedupe and survive
    # cross-session lookup. SHA-256 is overkill for a non-adversarial
    # use case but cheap and unambiguous.
    sha = hashlib.sha256(encoded).hexdigest()
    overflow_dir.mkdir(parents=True, exist_ok=True)
    out_path = overflow_dir / sha
    if not out_path.exists():
        # Write atomically-ish: identical content from any caller goes
        # to the same path, so a partial write from a crashed sibling
        # would be a rare and self-healing case (next call overwrites).
        out_path.write_bytes(encoded)

    # Walk back to the nearest valid UTF-8 boundary. UTF-8 continuation
    # bytes are 10xxxxxx (0x80–0xBF); a start byte is anything else.
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    head = encoded[:cut].decode("utf-8", errors="replace")

    return head, {
        "truncated": True,
        "overflow_sha256": sha,
        "original_bytes": len(encoded),
    }


def cap_command_data(
    data: dict[str, Any],
    *,
    max_bytes: int,
    overflow_dir: Path,
) -> dict[str, Any]:
    """Apply `cap_stream` to a command event's `stdout` and `stderr`.

    Returns a new dict so we don't mutate the caller's payload. Fields
    that aren't strings or aren't present are left alone. The metadata
    from `cap_stream` is merged in as `<field>_truncated`,
    `<field>_overflow_sha256`, `<field>_original_bytes`.
    """
    out = dict(data)
    for field, prefix in _CAPPED_FIELDS:
        value = out.get(field)
        if not isinstance(value, str):
            continue
        capped, meta = cap_stream(
            value, max_bytes=max_bytes, overflow_dir=overflow_dir
        )
        out[field] = capped
        for k, v in meta.items():
            out[f"{prefix}_{k}"] = v
    return out
