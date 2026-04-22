from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import ValidationError
import yaml

ROOT_CONFIG_FILENAME = "loopy_loop_config.yaml"
LOOPY_DIRNAME = ".loopy_loop"
WORKFLOWS_DIRNAME = "workflows"
GOAL_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_GOAL_CHECK_FAILURE_CAP = 3
DEFAULT_PROVIDER = "openai_compat"
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


class ConfigError(Exception):
    """Raised when config loading or validation fails."""


class RootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    goal_slug: str
    completion_criteria: list[str]
    stop_criteria: list[str]
    max_turns: int = Field(...)
    goal_check_consecutive_failures_cap: int = Field(
        default=DEFAULT_GOAL_CHECK_FAILURE_CAP, ge=1
    )
    team_harness_provider: str = Field(default=DEFAULT_PROVIDER)
    team_harness_model: str = Field(default=DEFAULT_MODEL)
    team_harness_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_AGENTS))
    team_harness_api_base: str = Field(default=DEFAULT_API_BASE)
    team_harness_api_key_env: str = Field(default=DEFAULT_API_KEY_ENV)
    team_harness_system_prompt_extension: str = Field(default=DEFAULT_SYSTEM_PROMPT_EXTENSION)

    @field_validator("goal_slug")
    @classmethod
    def validate_goal_slug(cls, value: str) -> str:
        if GOAL_SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("goal_slug must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return value

    @field_validator("completion_criteria", "stop_criteria")
    @classmethod
    def validate_string_lists(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("list must not be empty")
        return value

    @field_validator("team_harness_api_base")
    @classmethod
    def normalize_api_base_value(cls, value: str) -> str:
        return normalize_api_base(value=value)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=DEFAULT_WORKFLOW_ENABLED)
    run_every: int = Field(default=DEFAULT_WORKFLOW_RUN_EVERY, ge=1)
    must_follow: str | None = Field(default=DEFAULT_WORKFLOW_MUST_FOLLOW)
    not_before_iteration: int = Field(
        default=DEFAULT_WORKFLOW_NOT_BEFORE_ITERATION, ge=0
    )
    description: str = Field(default=DEFAULT_WORKFLOW_DESCRIPTION)


class WorkflowDefinition(WorkflowConfig):
    id: str = Field(...)
    directory: Path = Field(...)
    prompt_path: Path = Field(...)
    config_path: Path = Field(...)


class PreflightResult(BaseModel):
    root_config: RootConfig
    workflows: list[WorkflowDefinition]


def normalize_api_base(*, value: str) -> str:
    base = value.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


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
        if workflow.must_follow is None:
            continue
        if workflow.must_follow not in workflow_ids:
            unresolved.append(
                f"{workflow.id}: must_follow references missing workflow "
                f"'{workflow.must_follow}'"
            )
    if unresolved:
        joined = "\n".join(unresolved)
        raise ConfigError(f"Workflow graph validation failed:\n{joined}")


def resolve_api_key(*, config: RootConfig) -> str:
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
