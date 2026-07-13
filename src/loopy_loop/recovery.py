"""Crash recovery for a dead worker's orphaned agent processes (D7 / P2.5).

When the coordinator confirms a worker is dead and its iteration produced no
recoverable result, the agent CLIs that worker's harness spawned may still be
running — orphaned, spending money, writing to the checkout. team-harness owns
the mechanism (persisted process identity + drain/reap policies, TH-D5); this
module is the loopy-side trigger:

1. **Discover** the interrupted harness run(s): team-harness routes each run's
   session output under the iteration's ``harness_outputs`` directory, named by
   run id, so the run ids are the directory names — and each run's crash-durable
   ``run.json`` lives under team-harness's runs dir.
2. **Apply the recovery policy** via ``team_harness.tracking.reaper.reap_run``:
   ``drain`` (default — let in-flight agents finish within a shared bounded
   timeout, preserving near-complete work and a clean tree) or ``reap`` (kill).
3. **Record the salvage**: a ``salvage.json`` in the interrupted iteration's
   directory carrying the reap reports, so the provenance of any surviving
   working-tree edits is auditable (the iteration itself is still re-run — its
   ``result.json`` never existed and is never fabricated; loopy `decisions.md`
   D3/D7).

team-harness versions without the reaper are tolerated: recovery degrades to
the pre-existing behavior (mark abandoned, re-run) with nothing reaped.
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
from typing import Any

from loopy_loop.models import utc_now
from loopy_loop.sessions import iteration_dir_path
from loopy_loop.sessions import iteration_harness_output_root

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
    """What crash recovery did about a dead worker's orphaned agents."""

    reaped_runs: int = 0
    settled_workers: int = 0
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
) -> RecoveryOutcome:
    """Drain/reap the interrupted iteration's orphaned agents; write salvage.json.

    Raises ``RecoveryRefusedError`` when team-harness's parent-liveness guard
    finds the run's owning process still alive — the caller should treat that
    as "the previous worker is still running".
    """
    outcome = RecoveryOutcome()
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
    for run_id in _discover_run_ids(output_root):
        run_json = Path(th_config.RUNS_DIR) / run_id / "run.json"
        if not run_json.exists():
            logger.warning("no run.json for interrupted harness run %s", run_id)
            continue
        try:
            report = reaper.reap_run(
                run_json, policy=policy, drain_timeout_s=drain_timeout_s
            )
        except reaper.ReapRefusedError as exc:
            raise RecoveryRefusedError(str(exc)) from exc
        outcome.reaped_runs += 1
        outcome.settled_workers += sum(
            1 for worker in report.workers if worker.outcome in _SETTLED_OUTCOMES
        )
        outcome.reports.append(report.model_dump(mode="json"))
    if outcome.reaped_runs:
        _write_salvage_record(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
            outcome=outcome,
            policy=policy,
        )
    return outcome


def _discover_run_ids(output_root: Path) -> list[str]:
    """The iteration's harness output root contains one directory per run id."""
    if not output_root.is_dir():
        return []
    return sorted(entry.name for entry in output_root.iterdir() if entry.is_dir())


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

    The iteration is still re-run (D3: its result.json never existed and is
    never fabricated); surviving working-tree edits are explained by this
    record instead of appearing as a mystery diff.
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
        "reports": outcome.reports,
    }
    (iteration_dir / SALVAGE_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
