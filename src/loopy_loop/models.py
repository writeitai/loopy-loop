from __future__ import annotations

from datetime import datetime
from datetime import UTC
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
CONTROL_SCHEMA_VERSION = 1
GOAL_CHECK_SCHEMA_VERSION = 1
RUN_ACTION = "run"
STOP_ACTION = "stop"


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class RootConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(...)
    goal_hash: str = Field(...)
    completion_criteria: list[str] = Field(...)
    stop_criteria: list[str] = Field(...)
    max_turns: int = Field(...)
    goal_check_consecutive_failures_cap: int = Field(...)
    team_harness_provider: str = Field(...)
    team_harness_model: str = Field(...)
    team_harness_agents: list[str] = Field(...)
    team_harness_agent_models: dict[str, str] = Field(default_factory=dict)
    team_harness_agent_reasoning_efforts: dict[str, str] = Field(default_factory=dict)
    team_harness_api_base: str = Field(...)
    team_harness_api_key_env: str = Field(...)
    team_harness_system_prompt_extension: str = Field(...)


class CurrentTask(BaseModel):
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    iteration: int = Field(...)
    started_at: datetime = Field(...)


class TaskResponse(BaseModel):
    action: Literal["run", "stop"] = Field(...)
    workflow_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    iteration: int | None = Field(default=None)
    config_snapshot: RootConfigSnapshot | None = Field(default=None)
    stop_reason: str | None = Field(default=None)


class HistoryEntry(BaseModel):
    iteration: int = Field(...)
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    success: bool = Field(...)
    error: str | None = Field(default=None)
    started_at: datetime = Field(...)
    finished_at: datetime = Field(...)


class LoopState(BaseModel):
    status: Literal["running", "stopped", "goal_met", "failed", "max_turns"] = Field(
        default="running"
    )
    goal_hash: str = Field(...)
    max_turns: int = Field(...)
    active_session_id: str = Field(...)
    goal_met: bool = Field(default=False)
    stop_requested: bool = Field(default=False)
    unresolvable_error: bool = Field(default=False)
    stop_reason: str | None = Field(default=None)
    iteration_count: int = Field(default=0)
    goal_check_consecutive_failures: int = Field(default=0)
    current_task: CurrentTask | None = Field(default=None)
    history: list[HistoryEntry] = Field(default_factory=list)
    config_snapshot: RootConfigSnapshot = Field(...)


class FinishedRequest(BaseModel):
    workflow_id: str = Field(...)
    session_id: str = Field(...)
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
