"""Worker process identity: capture and verification (loopy side of TH-D5/D7).

The worker sends its identity (hostname + pid + a pid-reuse-proof start-time
token) with every /register and /finished call; the coordinator stamps it onto
the dispatched ``CurrentTask``. On a later /register while that task is still
current, the coordinator can then *verify* whether the recorded worker is
actually dead before reclaiming the task — instead of assuming abandonment —
closing the duplicate-work window (design: loopy `decisions.md` D7, proposals
P0.1).

The start-time token comes from team-harness's ``process_identity`` module
(the same mechanism its orphan reaper uses). team-harness versions without it
are tolerated: identity then carries no starttime and verification degrades to
"unknown", which preserves the pre-existing assume-abandoned behavior.
Verification is only meaningful on the coordinator's own host; identities from
another hostname are likewise "unknown".
"""

from __future__ import annotations

import importlib
import os
import socket

from loopy_loop.models import WorkerIdentity


def capture_starttime(pid: int) -> str | None:
    """team-harness's pid-reuse-proof start-time token; None if unavailable.

    Resolved at call time so loopy-loop keeps working with team-harness
    versions that predate process identity (< 0.2.11).
    """
    try:
        module = importlib.import_module("team_harness.agents.process_identity")
    except ImportError:
        return None
    return module.capture_starttime(pid)


def current_worker_identity() -> WorkerIdentity:
    """Identity of THIS process, captured for /register and /finished calls."""
    pid = os.getpid()
    return WorkerIdentity(
        hostname=socket.gethostname(), pid=pid, starttime=capture_starttime(pid)
    )


def is_worker_alive(identity: WorkerIdentity | None) -> bool | None:
    """Verified liveness of a recorded worker identity.

    Returns True/False only when the answer is *verified*: same host, a
    recorded start-time token, and a current capture that matches (alive) or
    differs/is absent (dead — the pid is gone or was recycled). Returns None
    when verification is impossible (no identity recorded, remote host, or no
    token) — callers must treat None as "unknown" and fall back to the
    pre-existing recovery behavior, never as "alive".
    """
    if identity is None or identity.starttime is None:
        return None
    if identity.hostname != socket.gethostname():
        return None
    current = capture_starttime(identity.pid)
    return current == identity.starttime
