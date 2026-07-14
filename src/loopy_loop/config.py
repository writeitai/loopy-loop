from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import computed_field
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from pydantic import ValidationError
import yaml

ROOT_CONFIG_FILENAME = "loopy_loop_config.yaml"
DEFAULT_GOAL_FILENAME = "loopy_loop_goal.txt"
LOOPY_DIRNAME = ".loopy_loop"
WORKFLOWS_DIRNAME = "workflows"
WORKFLOW_SETS_DIRNAME = "workflow_sets"
GOAL_HASH_LENGTH = 12
DEFAULT_GOAL_CHECK_FAILURE_CAP = 3
DEFAULT_WORKFLOW_FAILURE_CAP = 5
DEFAULT_PROVIDER = "openai_compat"
PROVIDERS_WITHOUT_API_KEY: frozenset[str] = frozenset({"codex"})
DEFAULT_MODEL = "gpt-5.5"
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


class ModelPrices(BaseModel):
    """USD prices per 1M tokens for the harness coordinator model."""

    model_config = ConfigDict(extra="forbid")

    prompt_usd_per_1m: float = Field(ge=0)
    completion_usd_per_1m: float = Field(ge=0)


class ModelTierSpec(BaseModel):
    """One agent's model/effort bundle inside a named tier.

    A tier bundles model and effort deliberately (instead of exposing them as
    independent axes) so workflow prompts and harness coordinators reason
    about one word — "economy", "strong" — not a model x effort grid.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Worker CLI model id for this agent.")
    effort: str | None = Field(
        default=None,
        description=(
            "Optional reasoning-effort level for this agent. Only agents "
            "whose CLI exposes a reasoning-effort flag will use it."
        ),
    )

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("effort must not be empty when set")
        return value


class RootConfig(BaseModel):
    """Repo-level loop configuration loaded from loopy_loop_config.yaml."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(
        description="Natural-language goal the loop is trying to satisfy."
    )
    workflow_set: str = Field(
        ...,
        description="Workflow set used when the coordinator is started without an override.",
    )
    completion_criteria: list[str] = Field(
        default_factory=list,
        description="Observable criteria used by workflows and goal checks.",
    )
    stop_criteria: list[str] = Field(
        default_factory=list,
        description="Conditions that should stop the loop before the goal is met.",
    )
    max_turns: int = Field(
        ..., description="Maximum number of completed workflow iterations."
    )
    goal_check_consecutive_failures_cap: int = Field(
        default=DEFAULT_GOAL_CHECK_FAILURE_CAP,
        ge=1,
        description="Consecutive invalid goal-check outputs allowed before failure.",
    )
    workflow_consecutive_failures_cap: int = Field(
        default=DEFAULT_WORKFLOW_FAILURE_CAP,
        ge=1,
        description=(
            "Consecutive failed iterations of the same workflow before the "
            "loop stops with stop_reason=workflow_failure_cap. Coordinator-"
            "side only; not part of the wire config snapshot."
        ),
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
    model_tiers: dict[str, dict[str, ModelTierSpec]] = Field(
        default_factory=dict,
        description=(
            "Named worker-model tiers (tier name -> agent name -> "
            "{model, effort}). Declared once here as the single source of "
            "truth for model ids; rendered into the harness system prompt so "
            "coordinators can pick a tier per spawned agent. Coordinator-side "
            "only; resolved into concrete fields at config load."
        ),
    )
    default_tier: str | None = Field(
        default=None,
        description=(
            "model_tiers key whose bundles become the per-agent spawn "
            "defaults (team_harness_agent_models / "
            "team_harness_agent_reasoning_efforts). Mutually exclusive with "
            "setting those mappings explicitly. Coordinator-side only."
        ),
    )
    team_harness_max_retries: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional coordinator retry budget passed to team-harness. Leave null "
            "to use the installed team-harness default."
        ),
    )
    team_harness_retry_base_delay_s: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional base delay in seconds for team-harness coordinator retry backoff."
        ),
    )
    team_harness_retry_max_delay_s: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional maximum delay in seconds for team-harness coordinator retry "
            "backoff."
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
    recovery_policy: Literal["drain", "reap"] = Field(
        default="drain",
        description=(
            "What to do with agent processes orphaned by a crashed worker: "
            "'drain' lets in-flight agents finish (bounded by "
            "recovery_drain_timeout_s) before re-running the iteration; "
            "'reap' kills them immediately."
        ),
    )
    max_cost_usd: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Stop the loop with stop_reason=max_cost_usd once the session "
            "tree's estimated coordinator-model cost reaches this budget. "
            "Requires model_prices. Coordinator-side only."
        ),
    )
    model_prices: ModelPrices | None = Field(
        default=None,
        description=(
            "USD prices per 1M tokens for the coordinator model; used to "
            "derive cost from the token ledger. Coordinator-side only."
        ),
    )
    recovery_drain_timeout_s: float = Field(
        default=600.0,
        ge=0,
        description=(
            "Shared deadline (seconds) for draining orphaned agents during "
            "crash recovery before they are killed."
        ),
    )

    @computed_field
    @property
    def goal_hash(self) -> str:
        return derive_goal_hash(goal=self.goal)

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

    @field_validator("model_tiers")
    @classmethod
    def validate_model_tiers_keys(
        cls, value: dict[str, dict[str, ModelTierSpec]]
    ) -> dict[str, dict[str, ModelTierSpec]]:
        for tier_name, agents in value.items():
            if not tier_name.strip():
                raise ValueError("model_tiers keys must not be empty")
            if not agents:
                raise ValueError(f"model_tiers[{tier_name!r}] must not be empty")
            for agent_name in agents:
                if not agent_name.strip():
                    raise ValueError(
                        f"model_tiers[{tier_name!r}] agent keys must not be empty"
                    )
        return value

    @field_validator("workflow_set")
    @classmethod
    def validate_workflow_set(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow_set must not be empty")
        return value

    @field_validator("team_harness_api_base")
    @classmethod
    def normalize_api_base_value(cls, value: str) -> str:
        return normalize_api_base(value=value)

    @model_validator(mode="after")
    def validate_model_tier_selection(self) -> "RootConfig":
        if self.default_tier is not None:
            if self.default_tier not in self.model_tiers:
                raise ValueError(
                    f"default_tier {self.default_tier!r} is not a model_tiers "
                    f"key; available tiers: {sorted(self.model_tiers)}"
                )
            if self.team_harness_agent_models or (
                self.team_harness_agent_reasoning_efforts
            ):
                raise ValueError(
                    "default_tier derives team_harness_agent_models and "
                    "team_harness_agent_reasoning_efforts from the named tier; "
                    "remove the explicit mappings so model ids have one source "
                    "of truth"
                )
        unknown_agents = {
            agent for agents in self.model_tiers.values() for agent in agents
        } - set(self.team_harness_agents)
        if unknown_agents:
            raise ValueError(
                "model_tiers references agents missing from "
                f"team_harness_agents: {sorted(unknown_agents)}"
            )
        return self

    @model_validator(mode="after")
    def validate_cost_budget(self) -> "RootConfig":
        if self.max_cost_usd is not None and self.model_prices is None:
            raise ValueError(
                "max_cost_usd requires model_prices: cost is derived from the "
                "token ledger and your configured prices — without prices the "
                "budget could never trigger"
            )
        return self

    @model_validator(mode="after")
    def validate_retry_delay_bounds(self) -> "RootConfig":
        if (
            self.team_harness_retry_base_delay_s is not None
            and self.team_harness_retry_max_delay_s is not None
            and self.team_harness_retry_max_delay_s
            < self.team_harness_retry_base_delay_s
        ):
            raise ValueError(
                "team_harness_retry_max_delay_s must be greater than or equal to "
                "team_harness_retry_base_delay_s"
            )
        return self


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

    workflow_set: str = Field(...)
    id: str = Field(...)
    directory: Path = Field(...)
    prompt_path: Path = Field(...)
    config_path: Path = Field(...)


class PreflightResult(BaseModel):
    """Validated root config and workflow definitions captured at startup."""

    root_config: RootConfig
    workflow_set: str
    workflows: list[WorkflowDefinition]


def estimate_cost_usd(
    *, prompt_tokens: int, completion_tokens: int, prices: ModelPrices | None
) -> float | None:
    """Estimated coordinator-model cost; None when no prices are configured."""
    if prices is None:
        return None
    return (
        prompt_tokens * prices.prompt_usd_per_1m
        + completion_tokens * prices.completion_usd_per_1m
    ) / 1_000_000


def normalize_api_base(*, value: str) -> str:
    base = value.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def derive_goal_hash(*, goal: str) -> str:
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:GOAL_HASH_LENGTH]


def load_root_config(*, repo_root: Path, goal_file: Path | None = None) -> RootConfig:
    config_path = repo_root / ROOT_CONFIG_FILENAME
    data = _read_yaml_mapping(path=config_path)
    if goal_file is not None:
        data["goal_file"] = str(goal_file)
    data = _resolve_root_config_goal(data=data, config_path=config_path)
    try:
        config = RootConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid root config at {config_path}: {exc}") from exc
    return resolve_model_tiers(config=config)


def resolve_model_tiers(*, config: RootConfig) -> RootConfig:
    """Resolve declared model tiers into the concrete harness fields.

    Applied once, on the config freshly loaded from YAML (never on a config
    reconstructed from a session snapshot — snapshots carry only the resolved
    values). Two effects when ``model_tiers`` is non-empty:

    - ``default_tier`` (if set) derives ``team_harness_agent_models`` /
      ``team_harness_agent_reasoning_efforts`` so per-agent spawn defaults
      and the tier table cannot drift apart.
    - A rendered tier-guidance block is appended to
      ``team_harness_system_prompt_extension`` so every harness coordinator
      in the session tree knows which named tiers exist and how to select
      one per spawned agent. This is guidance, not enforcement (D8):
      adherence is checked by reviewers via the harness audit trail, never
      gated.
    """
    if not config.model_tiers:
        return config
    update: dict[str, Any] = {}
    if config.default_tier is not None:
        tier = config.model_tiers[config.default_tier]
        update["team_harness_agent_models"] = {
            agent: spec.model for agent, spec in tier.items()
        }
        update["team_harness_agent_reasoning_efforts"] = {
            agent: spec.effort
            for agent, spec in tier.items()
            if spec.effort is not None
        }
    guidance = render_model_tier_guidance(config=config)
    extension = config.team_harness_system_prompt_extension
    update["team_harness_system_prompt_extension"] = (
        f"{extension.rstrip()}\n\n{guidance}" if extension.strip() else guidance
    )
    return config.model_copy(update=update)


def render_model_tier_guidance(*, config: RootConfig) -> str:
    """Render the coordinator-facing tier table from ``model_tiers``.

    Workflow prompts should refer to tier NAMES only; this block is the one
    place tier names expand to model ids, so model churn stays a one-line
    config edit.
    """
    lines = [
        "Model tier policy:",
        "- When spawning worker agents you may pass `model` (and `effort`, "
        "where the CLI supports it) per spawn to move a single task to a "
        "different tier.",
        "- Named tiers:",
    ]
    for tier_name, agents in config.model_tiers.items():
        parts: list[str] = []
        for agent_name, spec in agents.items():
            part = f"{agent_name} model={spec.model}"
            if spec.effort is not None:
                part += f" effort={spec.effort}"
            parts.append(part)
        lines.append(f"  - {tier_name}: " + "; ".join(parts))
    if config.default_tier is not None:
        lines.append(
            f"- Default tier: {config.default_tier}. It is already applied as "
            "the spawn defaults; omitting model/effort uses it."
        )
    lines.append(
        "- Choose a stronger tier for review, evaluation authoring, and "
        "planning work, or when your assignment names a tier; choose a "
        "cheaper tier for routine mechanical work."
    )
    return "\n".join(lines)


def load_workflow_config(*, workflow_dir: Path) -> WorkflowConfig:
    config_path = workflow_dir / "config.yaml"
    data = _read_yaml_mapping(path=config_path)
    try:
        return WorkflowConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid workflow config at {config_path}: {exc}") from exc


def workflow_set_dir_path(*, repo_root: Path, workflow_set: str) -> Path:
    return repo_root / LOOPY_DIRNAME / WORKFLOW_SETS_DIRNAME / workflow_set


def workflow_set_workflows_dir_path(*, repo_root: Path, workflow_set: str) -> Path:
    return workflow_set_dir_path(repo_root=repo_root, workflow_set=workflow_set) / (
        WORKFLOWS_DIRNAME
    )


def load_workflow_definitions(
    *, repo_root: Path, workflow_set: str
) -> list[WorkflowDefinition]:
    selected_workflow_set = workflow_set
    workflows_dir = workflow_set_workflows_dir_path(
        repo_root=repo_root, workflow_set=selected_workflow_set
    )
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
                    "workflow_set": selected_workflow_set,
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


def run_preflight(
    *, repo_root: Path, workflow_set: str | None = None, goal_file: Path | None = None
) -> PreflightResult:
    errors: list[str] = []
    root_config: RootConfig | None = None
    workflows: list[WorkflowDefinition] = []
    selected_workflow_set = workflow_set

    try:
        root_config = load_root_config(repo_root=repo_root, goal_file=goal_file)
        selected_workflow_set = workflow_set or root_config.workflow_set
        root_config = root_config.model_copy(
            update={"workflow_set": selected_workflow_set}
        )
    except ConfigError as exc:
        errors.append(str(exc))

    if selected_workflow_set is None:
        errors.append("No workflow_set specified in config or CLI override")
    else:
        try:
            workflows = load_workflow_definitions(
                repo_root=repo_root, workflow_set=selected_workflow_set
            )
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
        if selected_workflow_set is None:
            errors.append("No workflows found because no workflow_set was specified")
        else:
            legacy_workflows_dir = repo_root / LOOPY_DIRNAME / WORKFLOWS_DIRNAME
            if legacy_workflows_dir.exists():
                errors.append(
                    "Legacy workflow directory is not supported at runtime: "
                    f"{legacy_workflows_dir}. Move workflows under "
                    f"{repo_root / LOOPY_DIRNAME / WORKFLOW_SETS_DIRNAME}/"
                    "<workflow_set>/workflows/"
                )
            errors.append(
                "No workflows found under "
                f"{workflow_set_workflows_dir_path(repo_root=repo_root, workflow_set=selected_workflow_set)}"
            )

    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise ConfigError(f"Preflight failed:\n{joined}")

    assert root_config is not None
    assert selected_workflow_set is not None
    return PreflightResult(
        root_config=root_config, workflow_set=selected_workflow_set, workflows=workflows
    )


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


def _resolve_root_config_goal(
    *, data: dict[str, Any], config_path: Path
) -> dict[str, Any]:
    if "goal" in data:
        raise ConfigError(
            f"Invalid root config at {config_path}: field 'goal' is not supported; "
            "use 'goal_file' instead"
        )
    raw_goal_file = data.get("goal_file")
    if raw_goal_file is None:
        raise ConfigError(
            f"Invalid root config at {config_path}: missing required field 'goal_file'"
        )
    if not isinstance(raw_goal_file, str) or not raw_goal_file.strip():
        raise ConfigError(
            f"Invalid root config at {config_path}: goal_file must be a non-empty string"
        )
    goal_path = Path(raw_goal_file).expanduser()
    if not goal_path.is_absolute():
        goal_path = config_path.parent / goal_path
    try:
        goal = goal_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Unable to read goal file at {goal_path}: {exc}") from exc
    if not goal:
        raise ConfigError(f"Goal file at {goal_path} must not be empty")
    resolved = dict(data)
    resolved.pop("goal_file")
    resolved["goal"] = goal
    return resolved
