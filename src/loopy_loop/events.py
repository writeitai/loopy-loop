"""Session event log: the append-only ``events.jsonl`` stream (P1.1).

One versioned JSON object per line, one file per session (child sessions have
their own). This is the canonical operational stream: budgets, `loopy status
--watch`, `loopy events`, and any future TUI read this one truth instead of
inventing separate ones.

Emission semantics are at-least-once: events are appended AFTER the state
mutation that produced them commits, so a crash in between loses the event
(never the state), and a crash-replayed finalization can duplicate one.
Consumers key on ``event_id``. The reader tolerates a truncated final line
(a torn append from a crash) by skipping lines that do not decode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import uuid

from loopy_loop.models import utc_now
from loopy_loop.sessions import EVENTS_FILENAME
from loopy_loop.sessions import session_dir_path

EVENT_SCHEMA_VERSION = 1


def events_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id) / EVENTS_FILENAME
    )


def append_events(
    *, repo_root: Path, session_id: str, events: list[tuple[str, dict[str, Any]]]
) -> None:
    """Append (type, payload) pairs as complete lines in one write."""
    if not events:
        return
    path = events_path(repo_root=repo_root, session_id=session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().isoformat().replace("+00:00", "Z")
    lines = []
    for event_type, payload in events:
        lines.append(
            json.dumps(
                {
                    "event_id": uuid.uuid4().hex[:12],
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "ts": stamp,
                    "session_id": session_id,
                    "type": event_type,
                    "payload": payload,
                },
                separators=(",", ":"),
                default=str,
            )
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


def read_events(*, path: Path) -> list[dict[str, Any]]:
    """Read all decodable events; a torn final line is silently skipped."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return events
