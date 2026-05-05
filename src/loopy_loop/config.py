from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import computed_field
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import ValidationError
import yaml

ROOT_CONFIG_FILENAME = "loopy_loop_config.yaml"
LOOPY_DIRNAME = ".loopy_loop"
WORKFLOWS_DIRNAME = "workflows"
GOAL_HASH_LENGTH = 12
DEFAULT_GOAL_CHECK_FAILURE_CAP = 3
DEFAULT_PROVIDER = "openai_compat"
PROVIDERS_WITHOUT_API_KEY: frozenset[str] = frozenset({"codex"})
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_AGENTS = ["codex"]
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_SYSTEM_PROMPT_EXTENSION = ""
DEFAULT_WORKFLOW_ENABLED = True
DEFAULT_WORKFLOW_RUN_EVERY = 1
DEFAULT_WORKFLOW_MUST_FOLLOW: str | None = None
DEFAULT_WORKFLOW_NOT_BEFORE_ITERATION = 0
DEFAULT_WORKFLOW_DESCRIPTION = ""
DEFAULT_WORKFLOW_PRIORITY = 0
DEFAULT_WORKFLOW_RUN_ON_START = False
DEFAULT_WORKFLOW_EMITS_GOAL_CHECK = False


class ConfigError(Exception):
    """Raised when config loading or validation fails."""


class RootConfig(BaseModel):
    """Repo-level loop configuration loaded from loopy_loop_config.yaml."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(
        description="Natural-language goal the loop is trying to satisfy."
    )
    completion_criteria: list[str] = Field(
        description="Observable criteria used by workflows and goal checks."
    )
    stop_criteria: list[str] = Field(
        description="Conditions that should stop the loop before the goal is met."
    )
    max_turns: int = Field(
        ..., description="Maximum number of completed workflow iterations."
    )
    goal_check_consecutive_failures_cap: int = Field(
        default=DEFAULT_GOAL_CHECK_FAILURE_CAP,
        ge=1,
        description="Consecutive invalid goal-check outputs allowed before failure.",
    )
    team_harness_provider: str = Field(
        default=DEFAULT_PROVIDER,
        description="team-harness provider name used by workers.",
    )
    team_harness_model: str = Field(
        default=DEFAULT_MODEL, description="Model name passed to team-harness."
    )
    team_harness_agents: list[str] = Field(
        default_factory=lambda: list(DEFAULT_AGENTS),
        description="Agent names team-harness should make available.",
    )
    team_harness_agent_models: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-agent worker model overrides passed to team-harness, keyed by "
            "agent name such as codex or gemini."
        ),
    )
    team_harness_agent_reasoning_efforts: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-agent reasoning-effort overrides passed to team-harness, keyed "
            "by agent name. Only agents whose templates support a reasoning "
            "effort flag will use this value."
        ),
    )
    team_harness_api_base: str = Field(
        default=DEFAULT_API_BASE,
        description="OpenAI-compatible API base URL passed to team-harness.",
    )
    team_harness_api_key_env: str = Field(
        default=DEFAULT_API_KEY_ENV,
        description="Environment variable name containing the API key.",
    )
    team_harness_system_prompt_extension: str = Field(
        default=DEFAULT_SYSTEM_PROMPT_EXTENSION,
        description="Additional system prompt text appended for every harness run.",
    )

    @computed_field
    @property
    def goal_hash(self) -> str:
        return derive_goal_hash(goal=self.goal)

    @field_validator("completion_criteria", "stop_criteria")
    @classmethod
    def validate_string_lists(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("list must not be empty")
        return value

    @field_validator(
        "team_harness_agent_models", "team_harness_agent_reasoning_efforts"
    )
    @classmethod
    def validate_non_empty_string_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key.strip():
                raise ValueError("mapping keys must not be empty")
            if not item.strip():
                raise ValueError(f"mapping value for {key!r} must not be empty")
        return value

    @field_validator("team_harness_api_base")
    @classmethod
    def normalize_api_base_value(cls, value: str) -> str:
        return normalize_api_base(value=value)


class RunAfterSuccesses(BaseModel):
    """Cadence rule that unlocks a workflow after successful runs of another."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(
        ..., description="Workflow id whose successful runs drive this cadence."
    )
    every: int = Field(
        ..., ge=1, description="Run after this many new successful target runs."
    )


class WorkflowConfig(BaseModel):
    """Per-workflow scheduling and execution configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=DEFAULT_WORKFLOW_ENABLED,
        description="Whether this workflow can be scheduled.",
    )
    run_every: int = Field(
        default=DEFAULT_WORKFLOW_RUN_EVERY,
        ge=1,
        description="Minimum completed iterations between runs of this workflow.",
    )
    must_follow: str | None = Field(
        default=DEFAULT_WORKFLOW_MUST_FOLLOW,
        description="Required immediately previous successful workflow id.",
    )
    not_before_iteration: int = Field(
        default=DEFAULT_WORKFLOW_NOT_BEFORE_ITERATION,
        ge=0,
        description="Earliest completed iteration count where workflow is eligible.",
    )
    description: str = Field(
        default=DEFAULT_WORKFLOW_DESCRIPTION,
        description="Human-readable purpose of this workflow.",
    )
    priority: int = Field(
        default=DEFAULT_WORKFLOW_PRIORITY,
        description="Tie-breaker among eligible workflows; higher runs first.",
    )
    run_on_start: bool = Field(
        default=DEFAULT_WORKFLOW_RUN_ON_START,
        description="Allow this workflow before any successful workflow has run.",
    )
    run_after_successes: RunAfterSuccesses | None = Field(
        default=None,
        description="Optional cadence based on successful runs of another workflow.",
    )
    emits_goal_check: bool = Field(
        default=DEFAULT_WORKFLOW_EMITS_GOAL_CHECK,
        description="Whether this workflow is expected to write goal_check.json.",
    )


class WorkflowDefinition(WorkflowConfig):
    """Resolved workflow config plus its id and on-disk file locations."""

    id: str = Field(...)
    directory: Path = Field(...)
    prompt_path: Path = Field(...)
    config_path: Path = Field(...)


class PreflightResult(BaseModel):
    """Validated root config and workflow definitions captured at startup."""

    root_config: RootConfig
    workflows: list[WorkflowDefinition]


def normalize_api_base(*, value: str) -> str:
    base = value.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def derive_goal_hash(*, goal: str) -> str:
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:GOAL_HASH_LENGTH]


def load_root_config(*, repo_root: Path) -> RootConfig:
    config_path = repo_root / ROOT_CONFIG_FILENAME
    data = _read_yaml_mapping(path=config_path)
    try:
        return RootConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid root config at {config_path}: {exc}") from exc


def load_workflow_config(*, workflow_dir: Path) -> WorkflowConfig:
    config_path = workflow_dir / "config.yaml"
    data = _read_yaml_mapping(path=config_path)
    try:
        return WorkflowConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid workflow config at {config_path}: {exc}") from exc


def load_workflow_definitions(*, repo_root: Path) -> list[WorkflowDefinition]:
    workflows_dir = repo_root / LOOPY_DIRNAME / WORKFLOWS_DIRNAME
    if not workflows_dir.exists():
        return []
    definitions: list[WorkflowDefinition] = []
    for workflow_dir in sorted(
        path for path in workflows_dir.iterdir() if path.is_dir()
    ):
        config = load_workflow_config(workflow_dir=workflow_dir)
        prompt_path = workflow_dir / "prompt.txt"
        config_path = workflow_dir / "config.yaml"
        try:
            definition = WorkflowDefinition.model_validate(
                {
                    **config.model_dump(),
                    "id": workflow_dir.name,
                    "directory": workflow_dir,
                    "prompt_path": prompt_path,
                    "config_path": config_path,
                }
            )
        except ValidationError as exc:
            raise ConfigError(
                f"Invalid workflow definition for {workflow_dir.name}: {exc}"
            ) from exc
        definitions.append(definition)
    return definitions


def validate_workflow_graph(*, workflows: list[WorkflowDefinition]) -> None:
    workflow_ids = {workflow.id for workflow in workflows}
    unresolved: list[str] = []
    for workflow in workflows:
        if (
            workflow.must_follow is not None
            and workflow.must_follow not in workflow_ids
        ):
            unresolved.append(
                f"{workflow.id}: must_follow references missing workflow "
                f"'{workflow.must_follow}'"
            )
        if workflow.run_after_successes is None:
            continue
        if workflow.run_after_successes.workflow_id not in workflow_ids:
            unresolved.append(
                f"{workflow.id}: run_after_successes references missing workflow "
                f"'{workflow.run_after_successes.workflow_id}'"
            )
    if unresolved:
        joined = "\n".join(unresolved)
        raise ConfigError(f"Workflow graph validation failed:\n{joined}")


def resolve_api_key(*, config: RootConfig) -> str | None:
    if config.team_harness_provider in PROVIDERS_WITHOUT_API_KEY:
        return None
    value = os.environ.get(config.team_harness_api_key_env)
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {config.team_harness_api_key_env}"
        )
    return value


def run_preflight(*, repo_root: Path) -> PreflightResult:
    errors: list[str] = []
    root_config: RootConfig | None = None
    workflows: list[WorkflowDefinition] = []

    try:
        root_config = load_root_config(repo_root=repo_root)
    except ConfigError as exc:
        errors.append(str(exc))

    try:
        workflows = load_workflow_definitions(repo_root=repo_root)
    except ConfigError as exc:
        errors.append(str(exc))

    if workflows:
        for workflow in workflows:
            if not workflow.prompt_path.is_file():
                errors.append(f"Missing workflow prompt: {workflow.prompt_path}")
            if not workflow.config_path.is_file():
                errors.append(f"Missing workflow config: {workflow.config_path}")

    if root_config is not None:
        try:
            validate_workflow_graph(workflows=workflows)
        except ConfigError as exc:
            errors.append(str(exc))
        try:
            resolve_api_key(config=root_config)
        except ConfigError as exc:
            errors.append(str(exc))

    if not workflows:
        errors.append(
            f"No workflows found under {repo_root / LOOPY_DIRNAME / WORKFLOWS_DIRNAME}"
        )

    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise ConfigError(f"Preflight failed:\n{joined}")

    assert root_config is not None
    return PreflightResult(root_config=root_config, workflows=workflows)


def load_workflow_prompt(*, workflow: WorkflowDefinition) -> str:
    try:
        return workflow.prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Unable to read workflow prompt at {workflow.prompt_path}: {exc}"
        ) from exc


def _read_yaml_mapping(*, path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Unable to read config file at {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected mapping at {path}")
    return data
