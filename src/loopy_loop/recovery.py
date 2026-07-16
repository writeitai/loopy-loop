"""Recovery for an interrupted worker task's agent processes (D7 / P2.5).

When a prior task remains unacknowledged and its iteration produced no
recoverable result, the agent CLIs that worker's harness spawned may still be
running, spending money and writing to the checkout. team-harness owns the
mechanism (persisted process identity + drain/reap policies, TH-D5); this module
is the loopy-side trigger:

1. **Discover** the interrupted harness run(s): new caller-contract runs return
   and retain their canonical ``run.json`` below the attempt trace; legacy runs
   are still found through the iteration's ``harness_outputs`` directory and
   team-harness's historical global runs directory.
2. **Apply the recovery policy** via ``team_harness.tracking.reaper.reap_run``:
   ``drain`` (default — let in-flight agents finish within a shared bounded
   timeout, preserving near-complete work and a clean tree) or ``reap`` (kill).
3. **Record the salvage**: a ``salvage.json`` in the interrupted iteration's
   directory carrying the reap reports, so the provenance of any surviving
   working-tree edits is auditable. The interrupted task is abandoned and
   consumes a turn; its ``result.json`` never existed and is never fabricated,
   and normal scheduling continues only if no stop condition fires (loopy
   `decisions.md` D3/D7).

team-harness versions without the reaper are tolerated: recovery degrades to
the pre-existing behavior (mark abandoned, then continue normal scheduling if
allowed) with nothing reaped.
``ReapRefusedError`` — team-harness's own parent-liveness guard — bubbles up so
the coordinator can treat "the run's owner is still alive" as a busy signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import importlib
import json
import logging
from pathlib import Path
import time
from typing import Any

from loopy_loop.models import utc_now
from loopy_loop.sessions import iteration_dir_path
from loopy_loop.sessions import iteration_harness_output_root
from loopy_loop.sessions import traces_root_path
from loopy_loop.sessions import write_json_atomic

logger = logging.getLogger(__name__)

SALVAGE_FILENAME = "salvage.json"
SALVAGE_SCHEMA_VERSION = 1

# Outcomes that mean the orphaned work reached a settled end state (finished on
# its own, or was killed) — i.e. something real was handled during recovery.
_SETTLED_OUTCOMES = {
    "drained",
    "already_exited",
    "reaped",
    "drain_timed_out_then_reaped",
}

# Outcomes that mean an orphan MAY STILL BE RUNNING after recovery: the
# coordinator must not dispatch replacement work on top of a possibly-live
# writer. (identity_mismatch_skipped means OUR group is gone — settled-safe;
# no_process_identity is a pre-identity record and keeps the legacy
# redispatch behavior; left_running only occurs under the ignore policy.)
_UNSETTLED_OUTCOMES = {
    "identity_unverifiable_skipped",
    "probe_failed",
    "kill_failed_still_running",
}


class RecoveryIncompleteError(RuntimeError):
    """Recovery ran but orphaned agents may still be running.

    Dispatching replacement work now could put two writers on the checkout.
    The salvage record documents exactly which orphans are unresolved; an
    operator can kill them (or wait for them to exit) and register again.
    """


class RecoveryRefusedError(RuntimeError):
    """team-harness refused to reap: the run's owning process is still alive.

    Loopy-owned wrapper for team-harness's ``ReapRefusedError`` so callers
    have a stable type to catch regardless of the installed harness version.
    """


def _load_reaper() -> tuple[Any, Any] | None:
    """Resolve team-harness's reaper at call time (needs team-harness >= 0.2.11).

    Runtime resolution (not a static import) so loopy-loop keeps working —
    minus orphan recovery — against older team-harness versions.
    """
    try:
        reaper = importlib.import_module("team_harness.tracking.reaper")
        th_config = importlib.import_module("team_harness.config")
    except ImportError:
        return None
    return reaper, th_config


@dataclass
class RecoveryOutcome:
    """What crash recovery did about an interrupted task's agent processes."""

    reaped_runs: int = 0
    settled_workers: int = 0
    unsettled_workers: int = 0
    policy: str = ""
    reports: list[dict[str, Any]] = field(default_factory=list)

    @property
    def salvaged(self) -> bool:
        return self.settled_workers > 0


def recover_interrupted_iteration(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    policy: str,
    drain_timeout_s: float,
    attempt_id: str | None = None,
) -> RecoveryOutcome:
    """Drain/reap the interrupted iteration's orphaned agents; write salvage.json.

    Raises ``RecoveryRefusedError`` when team-harness's parent-liveness guard
    finds the run's owning process still alive — the caller should treat that
    as "the previous worker is still running".
    """
    outcome = RecoveryOutcome(policy=policy)
    loaded = _load_reaper()
    if loaded is None:
        logger.warning(
            "team-harness has no process reaper (needs >= 0.2.11); "
            "skipping orphan recovery for iteration %04d_%s",
            iteration,
            workflow_id,
        )
        return outcome
    reaper, th_config = loaded
    output_root = iteration_harness_output_root(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id=workflow_id,
    )
    # ONE deadline shared across every discovered run — the advertised
    # timeout bounds the whole recovery, not each run separately.
    deadline = time.monotonic() + drain_timeout_s
    try:
        for run_id, run_json, contract_kind in _discover_run_records(
            output_root=output_root,
            traces_root=traces_root_path(repo_root=repo_root),
            session_id=session_id,
            attempt_id=attempt_id,
            legacy_runs_root=Path(th_config.RUNS_DIR),
        ):
            if not run_json.exists():
                logger.warning("no run.json for interrupted harness run %s", run_id)
                continue
            if not _run_record_matches(
                run_json=run_json,
                output_root=output_root,
                run_id=run_id,
                session_id=session_id,
                attempt_id=attempt_id,
                contract_kind=contract_kind,
            ):
                logger.warning(
                    "run.json for %s does not reference this iteration's "
                    "output directory; skipping (stray directory name?)",
                    run_id,
                )
                continue
            try:
                report = reaper.reap_run(
                    run_json,
                    policy=policy,
                    drain_timeout_s=max(0.0, deadline - time.monotonic()),
                )
            except reaper.ReapRefusedError as exc:
                raise RecoveryRefusedError(str(exc)) from exc
            outcome.reaped_runs += 1
            outcome.settled_workers += sum(
                1 for worker in report.workers if worker.outcome in _SETTLED_OUTCOMES
            )
            outcome.unsettled_workers += sum(
                1 for worker in report.workers if worker.outcome in _UNSETTLED_OUTCOMES
            )
            outcome.reports.append(report.model_dump(mode="json"))
    finally:
        # The salvage record survives a mid-loop refusal: whatever was already
        # reaped/drained stays auditable even when this call raises.
        if outcome.reaped_runs:
            _write_salvage_record(
                repo_root=repo_root,
                session_id=session_id,
                iteration=iteration,
                workflow_id=workflow_id,
                outcome=outcome,
                policy=policy,
            )
    if outcome.unsettled_workers:
        raise RecoveryIncompleteError(
            f"{outcome.unsettled_workers} orphaned agent(s) of iteration "
            f"{iteration:04d}_{workflow_id} may still be running "
            "(unverifiable identity, probe failure, or a kill that did not "
            "land); refusing to dispatch replacement work. See salvage.json "
            "in the iteration directory, resolve the leftover processes, "
            "and register again."
        )
    return outcome


def _run_record_matches(
    *,
    run_json: Path,
    output_root: Path,
    run_id: str,
    session_id: str | None = None,
    attempt_id: str | None = None,
    contract_kind: str = "legacy",
) -> bool:
    """Guard against stray directory names: the run record must point back at
    this iteration's output directory before we act on it."""
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("run_id") != run_id:
        return False
    caller_context = payload.get("caller_context")
    if contract_kind == "caller":
        if not isinstance(caller_context, dict):
            return False
        if caller_context.get("session_id") != session_id:
            return False
        if attempt_id is not None and (
            caller_context.get("parent_attempt_id") != attempt_id
        ):
            return False
        return True
    recorded = payload.get("session_output_dir")
    if recorded is None:
        # Older team-harness run records don't carry it; fall back to the
        # directory-name match already established.
        return True
    return Path(recorded).resolve() == (output_root / run_id).resolve()


def _discover_run_ids(output_root: Path) -> list[str]:
    """The iteration's harness output root contains one directory per run id."""
    if not output_root.is_dir():
        return []
    return sorted(entry.name for entry in output_root.iterdir() if entry.is_dir())


def _discover_run_records(
    *,
    output_root: Path,
    traces_root: Path,
    session_id: str,
    attempt_id: str | None,
    legacy_runs_root: Path,
) -> list[tuple[str, Path, str]]:
    """Return explicit caller records first, then non-duplicate legacy records."""
    records: list[tuple[str, Path, str]] = []
    seen: set[Path] = set()
    if attempt_id and traces_root.is_dir():
        pattern = f"*/sessions/{session_id}/attempts/{attempt_id}/harness/*/run.json"
        for path in sorted(traces_root.glob(pattern)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            records.append((path.parent.name, path, "caller"))
    for run_id in _discover_run_ids(output_root=output_root):
        path = legacy_runs_root / run_id / "run.json"
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        records.append((run_id, path, "legacy"))
    return records


def _write_salvage_record(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    outcome: RecoveryOutcome,
    policy: str,
) -> None:
    """Make the salvage auditable: which orphans were handled, and how.

    The interrupted task is abandoned rather than synthesized (D3: its
    result.json never existed and is never fabricated); surviving working-tree
    edits are explained by this record instead of appearing as a mystery diff.
    """
    iteration_dir = iteration_dir_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id=workflow_id,
    )
    iteration_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SALVAGE_SCHEMA_VERSION,
        "recorded_at": utc_now().isoformat().replace("+00:00", "Z"),
        "policy": policy,
        "reaped_runs": outcome.reaped_runs,
        "settled_workers": outcome.settled_workers,
        "unsettled_workers": outcome.unsettled_workers,
        "reports": outcome.reports,
    }
    write_json_atomic(path=iteration_dir / SALVAGE_FILENAME, payload=payload)
