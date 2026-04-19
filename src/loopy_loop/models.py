from __future__ import annotations

from datetime import datetime
from datetime import UTC
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

DEFAULT_LEASE_SECONDS = 600
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_FINISHED_RETRY_ATTEMPTS = 3
DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS = 1.0
CONTROL_SCHEMA_VERSION = 1
GOAL_CHECK_SCHEMA_VERSION = 1
RUN_ACTION = "run"
WAIT_ACTION = "wait"
STOP_ACTION = "stop"


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class RootConfigSnapshot(BaseModel):
    goal: str = Field(...)
    goal_slug: str = Field(...)
    completion_criteria: list[str] = Field(...)
    stop_criteria: list[str] = Field(...)
    max_turns: int = Field(...)
    goal_check_consecutive_failures_cap: int = Field(...)
    model: str = Field(...)
    agents: list[str] = Field(...)
    api_base: str = Field(...)
    api_key_env: str = Field(...)
    system_prompt_extension: str = Field(...)


class WorkerState(BaseModel):
    status: Literal["idle", "busy"] = Field(default="idle")
    registered_at: datetime = Field(...)
    last_seen_at: datetime = Field(...)


class ActiveAssignment(BaseModel):
    assignment_id: str = Field(...)
    worker_id: str = Field(...)
    session_id: str = Field(...)
    iteration: int = Field(...)
    workflow_id: str = Field(...)
    assigned_at: datetime = Field(...)
    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS)


class HistoryEntry(BaseModel):
    assignment_id: str = Field(...)
    iteration: int = Field(...)
    workflow_id: str = Field(...)
    worker_id: str = Field(...)
    session_id: str = Field(...)
    success: bool = Field(...)
    error: str | None = Field(default=None)
    started_at: datetime = Field(...)
    finished_at: datetime = Field(...)


class LoopState(BaseModel):
    status: Literal["running", "stopped", "goal_met", "failed", "max_turns"] = Field(
        default="running"
    )
    goal_slug: str = Field(...)
    max_turns: int = Field(...)
    active_session_id: str = Field(...)
    goal_met: bool = Field(default=False)
    stop_requested: bool = Field(default=False)
    unresolvable_error: bool = Field(default=False)
    stop_reason: str | None = Field(default=None)
    iteration_count: int = Field(default=0)
    goal_check_consecutive_failures: int = Field(default=0)
    active_assignment: ActiveAssignment | None = Field(default=None)
    workers: dict[str, WorkerState] = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)
    config_snapshot: RootConfigSnapshot = Field(...)


class RegisterWorkerResponse(BaseModel):
    worker_id: str = Field(...)


class NextActionResponse(BaseModel):
    action: Literal["run", "wait", "stop"] = Field(...)
    stop_reason: str | None = Field(default=None)
    assignment_id: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    iteration: int | None = Field(default=None)
    config_snapshot: RootConfigSnapshot | None = Field(default=None)


class FinishedRequest(BaseModel):
    assignment_id: str = Field(...)
    session_id: str = Field(...)
    workflow_id: str = Field(...)
    success: bool = Field(...)
    text: str | None = Field(default=None)
    error: str | None = Field(default=None)


class ControlSignal(BaseModel):
    unresolvable_error: bool = Field(...)
    reason: str = Field(...)
    schema_version: int = Field(...)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != CONTROL_SCHEMA_VERSION:
            raise ValueError(f"schema_version must equal {CONTROL_SCHEMA_VERSION}")
        return value


class GoalCheckSignal(BaseModel):
    goal_met: bool = Field(...)
    reason: str = Field(...)
    schema_version: int = Field(default=GOAL_CHECK_SCHEMA_VERSION)


class IterationResult(BaseModel):
    success: bool = Field(...)
    text: str | None = Field(default=None)
    error: str | None = Field(default=None)
    harness_run_id: str = Field(default="")
