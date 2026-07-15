from __future__ import annotations

from datetime import datetime
from datetime import UTC
from typing import Literal
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
CONTROL_SCHEMA_VERSION = 1
GOAL_CHECK_SCHEMA_VERSION = 1
RUN_ACTION = "run"
STOP_ACTION = "stop"

# Failure taxonomy (P2.3). Derived from team-harness's structured failure
# detail where available:
# - "transient": the provider said retry (429/5xx/network) and team-harness's
#   own retries were already exhausted — a later iteration may succeed.
# - "deterministic": retrying the same thing cannot help (auth failure,
#   invalid config, 4xx).
# - "crash": the task was abandoned by the worker-crash recovery path
#   (abandoned / abandoned_after_<policy> entries); for a remote or otherwise
#   unverifiable identity, this does not prove the worker died.
# - "unknown": no classification signal (agent-process failures, unexpected
#   exceptions, results from pre-taxonomy versions).
FailureKind = Literal["transient", "deterministic", "crash", "unknown"]


class IterationUsage(BaseModel):
    """Coordinator-model token usage for one iteration (P1.1).

    Read by the worker from team-harness's run.json (per-turn usage records).
    Covers the harness COORDINATOR model only: agent-CLI subprocesses (codex,
    claude, gemini) bill through their own accounts and are not measurable
    here — absence of this object means usage is unknown, not zero.
    """

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    turns: int = Field(default=0, ge=0)
    # Coordinator turns whose response carried no usage record: non-zero means
    # the token subtotal above is a lower bound, not complete accounting.
    turns_without_usage: int = Field(default=0, ge=0)


class SessionUsageTotals(BaseModel):
    """Durable per-session usage ledger (own iterations only).

    Child sessions' totals are recorded on their children.json records at
    finalization; tree-wide numbers are derived by summing, never double-
    stored.
    """

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    iterations_with_usage: int = Field(default=0, ge=0)
    iterations_without_usage: int = Field(default=0, ge=0)
    duration_s: float = Field(default=0.0, ge=0)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class RootConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(...)
    goal_hash: str = Field(...)
    workflow_set: str = Field(...)
    completion_criteria: list[str] = Field(...)
    stop_criteria: list[str] = Field(...)
    max_turns: int = Field(...)
    goal_check_consecutive_failures_cap: int = Field(...)
    team_harness_provider: str = Field(...)
    team_harness_model: str = Field(...)
    team_harness_agents: list[str] = Field(...)
    team_harness_agent_models: dict[str, str] = Field(default_factory=dict)
    team_harness_agent_reasoning_efforts: dict[str, str] = Field(default_factory=dict)
    team_harness_max_retries: int | None = Field(default=None)
    team_harness_retry_base_delay_s: float | None = Field(default=None)
    team_harness_retry_max_delay_s: float | None = Field(default=None)
    team_harness_api_base: str = Field(...)
    team_harness_api_key_env: str = Field(...)
    team_harness_system_prompt_extension: str = Field(...)


class WorkerIdentity(BaseModel):
    """Durable identity of the worker process holding an assignment.

    Lets the coordinator *verify* whether that worker is still alive before
    reclaiming its task (instead of assuming abandonment), closing the
    duplicate-work window on a second /register. ``starttime`` is the
    team-harness process-identity token (pid-reuse-proof); verification is
    only possible on the coordinator's own host — remote workers fall back
    to the old assume-abandoned behavior.
    """

    hostname: str = Field(...)
    pid: int = Field(...)
    starttime: str | None = Field(default=None)


class RegisterRequest(BaseModel):
    worker: WorkerIdentity | None = Field(default=None)


class CurrentTask(BaseModel):
    workflow_set: str = Field(...)
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    iteration: int = Field(...)
    started_at: datetime = Field(...)
    worker: WorkerIdentity | None = Field(default=None)
    # Unique per dispatch: distinguishes a legitimate retry of
    # (session, workflow, iteration) from a late /finished of an OLD attempt
    # of the very same coordinates. None only on pre-attempt persisted state.
    attempt_id: str | None = Field(default=None)


class TaskResponse(BaseModel):
    action: Literal["run", "stop"] = Field(...)
    workflow_set: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    iteration: int | None = Field(default=None)
    attempt_id: str | None = Field(default=None)
    config_snapshot: RootConfigSnapshot | None = Field(default=None)
    stop_reason: str | None = Field(default=None)


class HistoryEntry(BaseModel):
    iteration: int = Field(...)
    workflow_set: str = Field(...)
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    success: bool = Field(...)
    error: str | None = Field(default=None)
    failure_kind: FailureKind | None = Field(default=None)
    started_at: datetime = Field(...)
    finished_at: datetime = Field(...)


class LoopState(BaseModel):
    status: Literal["running", "stopped", "goal_met", "failed", "max_turns"] = Field(
        default="running"
    )
    goal_hash: str = Field(...)
    workflow_set: str = Field(...)
    parent_session_id: str | None = Field(default=None)
    max_turns: int = Field(...)
    active_session_id: str = Field(...)
    goal_met: bool = Field(default=False)
    stop_requested: bool = Field(default=False)
    unresolvable_error: bool = Field(default=False)
    stop_reason: str | None = Field(default=None)
    iteration_count: int = Field(default=0)
    goal_check_consecutive_failures: int = Field(default=0)
    # Per-workflow circuit breaker (P2.3): consecutive failed iterations per
    # workflow id; reset by that workflow's next success. When any counter
    # reaches the coordinator's workflow_consecutive_failures_cap the loop
    # stops with stop_reason="workflow_failure_cap" instead of burning the
    # remaining turn budget on a wedged workflow.
    workflow_consecutive_failures: dict[str, int] = Field(default_factory=dict)
    usage_totals: SessionUsageTotals = Field(default_factory=SessionUsageTotals)
    # The durable session-stack pointer: while a child session is active, the
    # parent records WHICH child, so a restarted coordinator can walk the
    # chain to the deepest non-terminal session instead of silently resuming
    # the parent and orphaning the running child.
    active_child_session_id: str | None = Field(default=None)
    current_task: CurrentTask | None = Field(default=None)
    history: list[HistoryEntry] = Field(default_factory=list)
    config_snapshot: RootConfigSnapshot = Field(...)

    @model_validator(mode="after")
    def reconcile_usage_ledger(self) -> Self:
        """Self-heal a ledger that predates it (pre-P1.1 resumed sessions).

        Iterations completed before the ledger existed have unknown usage;
        without this, a resumed session reports zero unknown iterations and a
        newly configured max_cost_usd silently treats all prior spend as
        zero. Idempotent: counted iterations are never reclassified.
        """
        totals = self.usage_totals
        counted = totals.iterations_with_usage + totals.iterations_without_usage
        if counted < self.iteration_count:
            totals.iterations_without_usage += self.iteration_count - counted
        return self


class FinishedRequest(BaseModel):
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    iteration: int = Field(...)
    success: bool = Field(...)
    text: str | None = Field(default=None)
    error: str | None = Field(default=None)
    # Identity of the calling worker — the same worker will run the NEXT task
    # this response dispatches, so it is stamped onto that CurrentTask.
    worker: WorkerIdentity | None = Field(default=None)
    # Echo of TaskResponse.attempt_id; lets the coordinator reject a late
    # /finished from a superseded attempt of the same coordinates.
    attempt_id: str | None = Field(default=None)
    failure_kind: FailureKind | None = Field(default=None)
    usage: IterationUsage | None = Field(default=None)
    duration_s: float | None = Field(default=None, ge=0)


class ControlSignal(BaseModel):
    state: Literal["running", "stopped"] = Field(...)
    reason: str = Field(...)
    stop_reason: Literal["goal_met", "unresolvable_error"] | None = Field(default=None)
    schema_version: int = Field(...)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != CONTROL_SCHEMA_VERSION:
            raise ValueError(f"schema_version must equal {CONTROL_SCHEMA_VERSION}")
        return value

    @model_validator(mode="after")
    def validate_stop_reason(self) -> Self:
        if self.state == "running" and self.stop_reason is not None:
            raise ValueError("running control state must not set stop_reason")
        if self.state == "stopped" and self.stop_reason is None:
            raise ValueError("stopped control state must set stop_reason")
        return self


class GoalCheckSignal(BaseModel):
    goal_met: bool = Field(...)
    reason: str = Field(...)
    schema_version: int = Field(default=GOAL_CHECK_SCHEMA_VERSION)


class IterationResult(BaseModel):
    success: bool = Field(...)
    text: str | None = Field(default=None)
    error: str | None = Field(default=None)
    error_detail: dict[str, object] | None = Field(default=None)
    failure_kind: FailureKind | None = Field(default=None)
    usage: IterationUsage | None = Field(default=None)
    duration_s: float | None = Field(default=None, ge=0)
    harness_run_id: str = Field(default="")
    harness_output_dir: str = Field(default="")
    # Attempt provenance: without it, a stale result.json could complete a
    # NEW attempt right after its stale pending file was correctly rejected.
    attempt_id: str | None = Field(default=None)


class ChildSessionRequest(BaseModel):
    workflow_set: str = Field(...)
    goal: str = Field(...)
    schema_version: int = Field(default=1)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must equal 1")
        return value


class ChildSessionRecord(BaseModel):
    session_id: str = Field(...)
    workflow_set: str = Field(...)
    goal_hash: str = Field(...)
    status: str = Field(...)
    created_at: datetime = Field(...)
    completed_at: datetime | None = Field(default=None)
    stop_reason: str | None = Field(default=None)
    # Name of the child_requests/ file that produced this child. Makes the
    # dispatch scan idempotent across the crash window between recording the
    # child and unlinking the request: a request whose filename already
    # appears in children.json is never dispatched twice.
    request_file: str | None = Field(default=None)
