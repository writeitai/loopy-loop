from __future__ import annotations

from datetime import datetime
from datetime import UTC
import re
from typing import Any
from typing import Literal
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import JsonValue
from pydantic import model_validator

DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
CONTROL_SCHEMA_VERSION = 3
GOAL_CHECK_SCHEMA_VERSION = 2
WORKER_PROTOCOL_VERSION = 3
LOOPY_V2_WORKER_CAPABILITIES = frozenset(
    {"assignment_v1", "frozen_workflow_v1", "trace_manifest_v1"}
)
LOOPY_V3_WORKER_CAPABILITIES = frozenset(
    {
        "assignment_v2",
        "harness_capability_roster_v1",
        "orchestrator_control_v3",
        "scheduler_view_v1",
        "semantic_handoff_v1",
    }
)
LOOPY_WORKER_CAPABILITIES = LOOPY_V2_WORKER_CAPABILITIES | LOOPY_V3_WORKER_CAPABILITIES
REQUIRED_HARNESS_CAPABILITIES = frozenset(
    {
        "caller_run_record_v1",
        "coordinator_input_v1",
        "spawn_assignment_v1",
        "nested_caller_context_v1",
    }
)
REQUIRED_V2_WORKER_CAPABILITIES = (
    LOOPY_V2_WORKER_CAPABILITIES | REQUIRED_HARNESS_CAPABILITIES
)
REQUIRED_V3_HARNESS_CAPABILITIES = frozenset({"capability_roster_context_v1"})
REQUIRED_V3_WORKER_CAPABILITIES = (
    REQUIRED_V2_WORKER_CAPABILITIES
    | LOOPY_V3_WORKER_CAPABILITIES
    | REQUIRED_V3_HARNESS_CAPABILITIES
)
RUN_ACTION = "run"
STOP_ACTION = "stop"
SAFE_DURABLE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

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
    worker_protocol_version: int | None = Field(default=None, ge=1)
    capabilities: list[str] = Field(default_factory=list)
    repo_root: str | None = Field(default=None)
    repository_id: str | None = Field(default=None)


class WorkflowSnapshotDescriptor(BaseModel):
    schema_version: int = Field(default=1)
    session_id: str = Field(...)
    workflow_set: str = Field(...)
    workflow_id: str = Field(...)
    iteration: int = Field(..., ge=1)
    attempt_id: str = Field(...)
    snapshot_root: str = Field(...)
    workflow_config_path: str = Field(...)
    workflow_prompt_path: str = Field(...)
    workflow_contract_path: str = Field(...)
    root_config_snapshot_path: str = Field(...)
    workflow_config_sha256: str = Field(...)
    workflow_prompt_sha256: str = Field(...)
    workflow_contract_sha256: str = Field(...)
    root_config_snapshot_sha256: str = Field(...)


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
    workflow_snapshot: WorkflowSnapshotDescriptor | None = Field(default=None)
    repository_id: str | None = Field(default=None)
    # SHA-256 frozen by the coordinator when it materializes the assignment,
    # before the worker or any harness agent can observe that file.
    assignment_sha256: str | None = Field(default=None)
    # v2 means /finished must echo the full repository/assignment/owner
    # binding. Legacy/direct API fixtures remain readable as v1 tasks.
    completion_contract_version: int = Field(default=1, ge=1)


class TaskResponse(BaseModel):
    action: Literal["run", "stop"] = Field(...)
    workflow_set: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    iteration: int | None = Field(default=None)
    attempt_id: str | None = Field(default=None)
    config_snapshot: RootConfigSnapshot | None = Field(default=None)
    stop_reason: str | None = Field(default=None)
    coordinator_protocol_version: int | None = Field(default=None, ge=1)
    required_capabilities: list[str] = Field(default_factory=list)
    repo_root: str | None = Field(default=None)
    repository_id: str | None = Field(default=None)
    assignment_path: str | None = Field(default=None)
    assignment_sha256: str | None = Field(default=None)
    workflow_snapshot: WorkflowSnapshotDescriptor | None = Field(default=None)


class WorkflowRoleContract(BaseModel):
    responsibility: str


class WorkflowEvalContract(BaseModel):
    """Legacy v1/v2 evaluation ownership retained for frozen sessions."""

    author_role: str | None = None
    runner_role: str | None = None
    goal_control_role: str | None = None


class WorkflowOrchestrationContract(BaseModel):
    """Durable semantic owners for a protocol-v3 session layer."""

    model_config = ConfigDict(extra="forbid")

    completion_role: str
    plan_owner: str
    handoff_owner: str
    task_acceptance_owner: str | None = None
    child_acceptance_owner: str | None = None


class WorkflowEvaluationContract(BaseModel):
    """Optional evaluation producers authorized by a v3 workflow set."""

    model_config = ConfigDict(extra="forbid")

    advisory: bool = True
    check_author_roles: list[str] = Field(default_factory=list)
    check_runner_roles: list[str] = Field(default_factory=list)


class WorkflowSetContract(BaseModel):
    """Frozen role and protocol contract for one durable session layer."""

    schema_version: int = Field(default=1)
    session_protocol_version: Literal[1, 2, 3] = 1
    layer_kind: str = "work"
    roles: dict[str, WorkflowRoleContract]
    state: list[dict[str, Any]] = Field(default_factory=list)
    eval: WorkflowEvalContract = Field(default_factory=WorkflowEvalContract)
    orchestration: WorkflowOrchestrationContract | None = None
    evaluation: WorkflowEvaluationContract = Field(
        default_factory=WorkflowEvaluationContract
    )
    task_acceptance_role: str | None = None
    terminal_blocker_reporting_roles: list[str] = Field(default_factory=list)
    child_interface: Literal["none", "recursive"] = "recursive"

    @model_validator(mode="after")
    def validate_role_references(self) -> Self:
        """Require v3 semantic authorities to name declared workflow roles."""

        if self.session_protocol_version < 3:
            return self
        if self.orchestration is None:
            raise ValueError("protocol v3 requires an orchestration contract")
        if not self.evaluation.advisory:
            raise ValueError("protocol v3 evaluation must remain advisory")
        orchestrator = self.orchestration.completion_role
        if (
            self.orchestration.plan_owner != orchestrator
            or self.orchestration.handoff_owner != orchestrator
        ):
            raise ValueError(
                "protocol v3 requires one role to own completion, plan, and handoff"
            )
        referenced = {
            orchestrator,
            self.orchestration.plan_owner,
            self.orchestration.handoff_owner,
            *self.evaluation.check_author_roles,
            *self.evaluation.check_runner_roles,
            *self.terminal_blocker_reporting_roles,
        }
        if self.orchestration.task_acceptance_owner is not None:
            referenced.add(self.orchestration.task_acceptance_owner)
        if self.orchestration.child_acceptance_owner is not None:
            referenced.add(self.orchestration.child_acceptance_owner)
        missing = sorted(referenced - set(self.roles))
        if missing:
            raise ValueError(f"workflow contract references unknown roles: {missing}")
        if any(
            value is not None
            for value in (
                self.eval.author_role,
                self.eval.runner_role,
                self.eval.goal_control_role,
            )
        ):
            raise ValueError("protocol v3 must use evaluation, not legacy eval owners")
        return self

    @property
    def completion_role(self) -> str | None:
        """Return the version-appropriate successful-control owner."""

        if self.session_protocol_version >= 3 and self.orchestration is not None:
            return self.orchestration.completion_role
        return self.eval.goal_control_role

    @property
    def check_runner_roles(self) -> list[str]:
        """Return roles allowed to produce evaluation receipts."""

        if self.session_protocol_version >= 3:
            return self.evaluation.check_runner_roles
        return [self.eval.runner_role] if self.eval.runner_role is not None else []

    @property
    def check_author_roles(self) -> list[str]:
        """Return roles allowed to author evaluation checks."""

        if self.session_protocol_version >= 3:
            return self.evaluation.check_author_roles
        return [self.eval.author_role] if self.eval.author_role is not None else []


class HarnessCapabilityBundle(BaseModel):
    """One configured family/tier worker bundle in the frozen roster."""

    model_config = ConfigDict(extra="forbid")

    available: bool
    model: str | None = None
    effort: str | None = None
    source: Literal[
        "configured_tier", "configured_default", "harness_default", "unavailable"
    ]


class HarnessCapabilityRoster(BaseModel):
    """Tree-wide enabled harness families and semantic strength mappings."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    root_session_id: str
    root_execution_config_sha256: str
    created_at: datetime
    coordinator: dict[str, str]
    tiers: dict[str, str]
    harnesses: dict[str, dict[str, HarnessCapabilityBundle]]
    default_tier: str | None = None


class WorkflowRosterRole(BaseModel):
    """One scheduled durable role in a session-frozen workflow roster."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    responsibility: str
    cadence: dict[str, Any]
    expected_outputs: list[str] = Field(default_factory=list)
    authorities: list[str] = Field(default_factory=list)


class WorkflowRoster(BaseModel):
    """Inspectable session-wide roster derived from frozen workflows."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    workflow_contract_sha256: str
    created_at: datetime
    completion_role: str | None = None
    roles: list[WorkflowRosterRole]


class SchedulerForecast(BaseModel):
    """Conditional next-role projection and its explicit assumptions."""

    model_config = ConfigDict(extra="forbid")

    next_workflow_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SchedulerView(BaseModel):
    """Attempt-frozen mechanical history and conditional scheduler context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    state_revision: int = Field(ge=0)
    attempt_id: str
    workflow_roster_sha256: str
    history_watermark: int = Field(ge=0)
    captured_at: datetime
    recent_history: list[dict[str, Any]] = Field(default_factory=list)
    conditional_forecast: SchedulerForecast


class HandoffProducer(BaseModel):
    """Workflow attempt that authored a semantic layer handoff."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    attempt_id: str


class LayerHandoff(BaseModel):
    """Compact rolling semantic summary owned by a layer orchestrator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    goal_sha256: str
    revision: int = Field(ge=0)
    producer: HandoffProducer | None = None
    summary: str = ""
    accepted_outcomes: list[Any] = Field(default_factory=list)
    open_work: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    decision_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    delivery_refs: list[str] = Field(default_factory=list)
    eval_refs: list[str] = Field(default_factory=list)
    updated_at: datetime


class EvalSubject(BaseModel):
    """Session and repository subject evaluated by one receipt."""

    root_session_id: str
    session_id: str
    goal_hash: str
    git_commit: str | None = None
    dirty_tree_digest: str | None = None


class EvalProducer(BaseModel):
    """Exact workflow attempt and harness run that produced an evaluation."""

    workflow_id: str
    iteration: int
    attempt_id: str
    harness_run_id: str

    @field_validator("harness_run_id")
    @classmethod
    def require_harness_run_id(cls, value: str) -> str:
        """Reject eval producers that lack a concrete harness run."""

        if not value.strip():
            raise ValueError("eval producer harness_run_id must not be blank")
        return value


class AcceptedEvalReceiptSeal(BaseModel):
    """Engine acceptance of a receipt after raw provenance validation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    receipt_ref: str
    receipt_sha256: str
    subject: EvalSubject
    producer: EvalProducer
    evaluated_git: dict[str, str | None]
    accepted_at: datetime


class AcceptedTerminalControlSnapshot(BaseModel):
    """Engine-owned bytes and parsed payload of one accepted v3 control."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    payload: dict[str, JsonValue]
    raw_json: str
    sha256: str
    accepted_at: datetime


class AcceptedHandoffSnapshot(BaseModel):
    """Last provenance-valid handoff bytes observed by the coordinator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    handoff: LayerHandoff
    raw_json: str
    sha256: str
    accepted_at: datetime


class OutcomeHandoff(BaseModel):
    """Observed state of the semantic handoff at terminal transition."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "missing", "invalid", "non_monotonic"]
    ref: str | None = None
    sha256: str | None = None
    revision: int | None = Field(default=None, ge=0)


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
    attempt_id: str | None = Field(default=None)
    harness_run_id: str | None = Field(default=None)
    assignment_sha256: str | None = Field(default=None)
    finished_request_sha256: str | None = Field(default=None)
    finished_response_sha256: str | None = Field(default=None)
    # v2 durable evidence uses a relocatable logical reference. The absolute
    # path remains only for reading pre-v2 history during migration.
    trace_manifest_ref: str | None = Field(default=None)
    trace_manifest_path: str | None = Field(default=None)


class LoopState(BaseModel):
    # Missing means legacy v1. Fresh contract sessions set v2 explicitly;
    # readers must not silently reinterpret pre-versioned crash projections.
    schema_version: int = Field(default=1, ge=1)
    state_revision: int = Field(default=0, ge=0)
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
    root_session_id: str | None = Field(default=None)
    depth: int = Field(default=0, ge=0)
    request_id: str | None = Field(default=None)
    work_item_id: str | None = Field(default=None)
    control_protocol_consecutive_failures: int = Field(default=0, ge=0)
    # Engine-owned trust root for the complete session role/protocol contract.
    # Agent-visible session.json and workflow_contract.json are projections;
    # they cannot replace this durable value between attempts.
    workflow_contract: WorkflowSetContract | None = Field(default=None)
    # Protocol-v3 session-frozen role/cadence context. The on-disk roster is
    # an agent-visible projection restored from this engine-owned value.
    workflow_roster: WorkflowRoster | None = Field(default=None)
    # Protocol-v3 tree-wide delegate catalog. The root freezes it from config;
    # descendants inherit the same exact payload rather than re-reading YAML.
    harness_capability_roster: HarnessCapabilityRoster | None = Field(default=None)
    # Last structurally valid handoff observed after a completed v3 attempt.
    # This is diagnostic continuity only; it never gates scheduling/control.
    handoff_revision: int = Field(default=0, ge=0)
    handoff_sha256: str | None = Field(default=None)
    accepted_eval_receipt_seals: dict[str, AcceptedEvalReceiptSeal] = Field(
        default_factory=dict
    )
    # Protocol-v3 terminal projections are rebuilt only from these
    # engine-owned acceptance snapshots, never by trusting mutable files after
    # the terminal transition.
    accepted_terminal_control: AcceptedTerminalControlSnapshot | None = Field(
        default=None
    )
    accepted_handoff_snapshot: AcceptedHandoffSnapshot | None = Field(default=None)
    latest_handoff_observation: OutcomeHandoff | None = Field(default=None)
    terminal_state_revision: int | None = Field(default=None, ge=0)
    terminal_at: datetime | None = Field(default=None)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        """Accept only state schemas supported by this coordinator."""

        if value not in {1, 2}:
            raise ValueError(f"unsupported loop state schema_version: {value}")
        return value

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
        if self.root_session_id is None:
            self.root_session_id = self.active_session_id
        return self

    @property
    def phase(self) -> Literal["ready", "executing", "suspended", "terminal"]:
        """Return the scheduling phase implied by the durable loop state."""

        if self.status in {"stopped", "goal_met", "failed", "max_turns"}:
            return "terminal"
        if self.current_task is not None:
            return "executing"
        if self.active_child_session_id is not None:
            return "suspended"
        return "ready"


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
    repository_id: str | None = Field(default=None)
    assignment_sha256: str | None = Field(default=None)
    harness_run_id: str | None = Field(default=None)
    trace_manifest_path: str | None = Field(default=None)
    trace_incomplete: bool = Field(default=False)
    trace_error: str | None = Field(default=None)


class SignalProducer(BaseModel):
    session_id: str
    workflow_id: str
    attempt_id: str


class ControlSignal(BaseModel):
    state: Literal["running", "stopped"] = Field(...)
    reason: str = Field(...)
    stop_reason: Literal["goal_met", "unresolvable_error"] | None = Field(default=None)
    schema_version: int = Field(...)
    control_id: str | None = Field(default=None)
    producer: SignalProducer | None = Field(default=None)
    eval_receipt_ref: str | None = Field(default=None)
    eval_receipt_refs: list[str] = Field(default_factory=list)
    handoff_ref: str | None = Field(default=None)
    attempted_routes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime | None = Field(default=None)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        """Accept every explicitly supported control schema."""

        if value not in {1, 2, CONTROL_SCHEMA_VERSION}:
            raise ValueError(
                f"schema_version must be 1, 2, or {CONTROL_SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def validate_stop_reason(self) -> Self:
        """Validate control state, terminal reason, and v2 evidence fields."""

        if self.state == "running" and self.stop_reason is not None:
            raise ValueError("running control state must not set stop_reason")
        if self.state == "stopped" and self.stop_reason is None:
            raise ValueError("stopped control state must set stop_reason")
        if self.schema_version in {2, 3} and self.state == "stopped":
            if (
                self.control_id is None
                or self.producer is None
                or self.created_at is None
            ):
                raise ValueError(
                    "identity-bound stopped control requires control_id, producer, "
                    "and created_at"
                )
            if not SAFE_DURABLE_ID_PATTERN.fullmatch(self.control_id):
                raise ValueError(
                    "identity-bound control_id must be a filesystem-safe identifier"
                )
            if not self.reason.strip():
                raise ValueError("terminal control reason must be nonblank")
        if self.schema_version == 2 and self.state == "stopped":
            if self.stop_reason == "goal_met" and self.eval_receipt_ref is None:
                raise ValueError("v2 goal_met control requires eval_receipt_ref")
            if self.eval_receipt_refs or self.handoff_ref is not None:
                raise ValueError("v2 control must not set v3 reference fields")
            if self.stop_reason == "unresolvable_error":
                if not self.attempted_routes:
                    raise ValueError("v2 unresolvable_error requires attempted_routes")
                if any(not route.strip() for route in self.attempted_routes):
                    raise ValueError(
                        "v2 unresolvable_error attempted_routes must be nonblank"
                    )
                if self.eval_receipt_ref is not None:
                    raise ValueError(
                        "v2 unresolvable_error must not set eval_receipt_ref"
                    )
        if self.schema_version == 3 and self.state == "stopped":
            if self.eval_receipt_ref is not None:
                raise ValueError("v3 control must use plural eval_receipt_refs")
            if any(not reference.strip() for reference in self.eval_receipt_refs):
                raise ValueError("v3 eval receipt references must be nonblank")
            if self.handoff_ref is not None and not self.handoff_ref.strip():
                raise ValueError("v3 handoff_ref must be nonblank when set")
            if self.stop_reason == "unresolvable_error":
                if not self.attempted_routes:
                    raise ValueError("v3 unresolvable_error requires attempted_routes")
                if any(not route.strip() for route in self.attempted_routes):
                    raise ValueError(
                        "v3 unresolvable_error attempted_routes must be nonblank"
                    )
                if self.eval_receipt_refs or self.handoff_ref is not None:
                    raise ValueError(
                        "v3 unresolvable_error must not cite eval or handoff refs"
                    )
        return self


class GoalCheckSignal(BaseModel):
    goal_met: bool = Field(...)
    reason: str = Field(...)
    schema_version: int = Field(default=1)
    eval_receipt_ref: str | None = Field(default=None)

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        """Require a receipt reference for identity-bound goal checks."""

        if self.schema_version not in {1, GOAL_CHECK_SCHEMA_VERSION}:
            raise ValueError("goal_check schema_version must be 1 or 2")
        if self.schema_version == 2 and self.eval_receipt_ref is None:
            raise ValueError("goal_check v2 requires eval_receipt_ref")
        return self


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
    harness_run_json_path: str = Field(default="")
    trace_manifest_path: str = Field(default="")
    # Attempt provenance: without it, a stale result.json could complete a
    # NEW attempt right after its stale pending file was correctly rejected.
    attempt_id: str | None = Field(default=None)
    # Completion-binding provenance is duplicated into result.json before the
    # worker posts /finished.  If the worker dies after writing the result but
    # before writing pending_finished_request.json, recovery can therefore
    # apply the same owner/repository/assignment fence as the live endpoint.
    worker: WorkerIdentity | None = Field(default=None)
    repository_id: str | None = Field(default=None)
    assignment_sha256: str | None = Field(default=None)
    trace_incomplete: bool = Field(default=False)
    trace_error: str | None = Field(default=None)


class ChildRequestOrigin(BaseModel):
    parent_attempt_id: str | None = None
    parent_work_item_id: str | None = None
    supersedes_request_id: str | None = None


class ChildAssignmentContract(BaseModel):
    goal: str
    completion_criteria: list[str] = Field(default_factory=list)
    stop_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)


class ArtifactInputRef(BaseModel):
    ref: str
    sha256: str


class ChildSessionRequest(BaseModel):
    workflow_set: str = Field(...)
    goal: str | None = Field(default=None)
    schema_version: int = Field(default=1)
    request_id: str | None = Field(default=None)
    origin: ChildRequestOrigin | None = Field(default=None)
    assignment: ChildAssignmentContract | None = Field(default=None)
    inputs: list[ArtifactInputRef] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        """Accept supported child-request schema versions."""

        if value not in {1, 2}:
            raise ValueError("schema_version must equal 1 or 2")
        return value

    @model_validator(mode="after")
    def validate_request_shape(self) -> Self:
        """Validate the fields required by each child-request schema."""

        if self.schema_version == 1:
            if self.goal is None or not self.goal.strip():
                raise ValueError("v1 child request requires goal")
        else:
            if self.request_id is None or not self.request_id.strip():
                raise ValueError("v2 child request requires request_id")
            if not SAFE_DURABLE_ID_PATTERN.fullmatch(self.request_id):
                raise ValueError("v2 child request_id is not filesystem-safe")
            if self.origin is None or self.assignment is None:
                raise ValueError("v2 child request requires origin and assignment")
            if not self.origin.parent_attempt_id or not (
                self.origin.parent_attempt_id.strip()
            ):
                raise ValueError("v2 child request requires origin.parent_attempt_id")
            if not self.assignment.goal.strip():
                raise ValueError("v2 child assignment goal must not be empty")
        return self

    @property
    def effective_goal(self) -> str:
        """Return the structured assignment goal or the legacy goal."""

        if self.assignment is not None:
            return self.assignment.goal
        assert self.goal is not None
        return self.goal


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
    request_id: str | None = Field(default=None)
    outcome_ref: str | None = Field(default=None)
    usage: SessionUsageTotals | None = Field(default=None)
    accepted_request_ref: str | None = Field(default=None)
    accepted_request_sha256: str | None = Field(default=None)
    parent_attempt_id: str | None = Field(default=None)
    parent_work_item_id: str | None = Field(default=None)
    goal_contract_hash: str | None = Field(default=None)
    dispatch_failures: list[dict[str, Any]] = Field(default_factory=list)


class SessionManifest(BaseModel):
    schema_version: int = Field(default=2)
    session_id: str
    root_session_id: str
    parent_session_id: str | None = None
    depth: int = Field(ge=0)
    workflow_set: str
    layer_kind: str
    goal_hash: str
    goal_contract_hash: str
    workflow_contract_hash: str
    origin: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class GoalContract(BaseModel):
    schema_version: int = Field(default=1)
    session_id: str
    goal: str
    goal_hash: str
    completion_criteria: list[str] = Field(default_factory=list)
    stop_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    terminal_blocker_policy_ref: str
    origin_request_id: str | None = None
    accepted_request_ref: str | None = None
    accepted_request_sha256: str | None = None
    inputs: list[ArtifactInputRef] = Field(default_factory=list)
    created_at: datetime


class EvalCheckDefinitionRef(BaseModel):
    check_id: str
    definition_sha256: str
    kind: Literal["harness_judge"]


class EvalCheckResult(BaseModel):
    check_id: str
    passed: bool
    reason: str


class EvalVerdict(BaseModel):
    goal_met: bool
    reason: str


class EvalJudge(BaseModel):
    provider: str
    model: str
    reasoning_effort: str

    @model_validator(mode="after")
    def require_effective_values(self) -> Self:
        """Require every effective judge setting to be nonblank."""

        if not all(
            value.strip()
            for value in (self.provider, self.model, self.reasoning_effort)
        ):
            raise ValueError("effective judge fields must not be blank")
        return self


class EvalReceipt(BaseModel):
    schema_version: int = Field(default=1)
    eval_id: str
    subject: EvalSubject
    producer: EvalProducer
    checks: list[EvalCheckDefinitionRef]
    judge: EvalJudge
    check_results: list[EvalCheckResult]
    verdict: EvalVerdict
    canonical_report_ref: str
    canonical_report_sha256: str
    raw_report_refs: list[str] = Field(default_factory=list)
    raw_report_sha256s: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> Self:
        """Validate receipt identities, digests, checks, and verdict agreement."""

        if self.schema_version != 1:
            raise ValueError("eval receipt schema_version must equal 1")
        if not SAFE_DURABLE_ID_PATTERN.fullmatch(self.eval_id):
            raise ValueError("eval receipt ID must be a filesystem-safe identifier")
        check_ids = [item.check_id for item in self.checks]
        result_ids = [item.check_id for item in self.check_results]
        if any(
            not SAFE_DURABLE_ID_PATTERN.fullmatch(check_id) for check_id in check_ids
        ):
            raise ValueError("eval check IDs must be filesystem-safe identifiers")
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("eval receipt check IDs must be unique")
        if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(check_ids):
            raise ValueError("eval receipt results must match its check inventory")
        if self.verdict.goal_met and not self.checks:
            raise ValueError("passing eval receipt must contain at least one check")
        if self.verdict.goal_met != all(item.passed for item in self.check_results):
            raise ValueError("eval receipt verdict contradicts its check results")
        for item in self.checks:
            if not _is_full_sha256(value=item.definition_sha256):
                raise ValueError("eval check definition digest must be full sha256")
        if not _is_full_sha256(value=self.canonical_report_sha256):
            raise ValueError("canonical eval report digest must be full sha256")
        if not self.raw_report_refs:
            raise ValueError("eval receipt must retain at least one raw report")
        if set(self.raw_report_sha256s) != set(self.raw_report_refs):
            raise ValueError("raw eval report digests must match raw report refs")
        if any(
            not _is_full_sha256(value=value)
            for value in self.raw_report_sha256s.values()
        ):
            raise ValueError("raw eval report digest must be full sha256")
        return self


class OutcomeArtifactRef(BaseModel):
    """Logical reference and digest captured in a terminal outcome."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    sha256: str


class OutcomeFallbackSummary(BaseModel):
    """Factual control or engine reason used when no valid handoff exists."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["control_reason", "engine_stop_reason"]
    text: str


class SessionOutcome(BaseModel):
    """Topology-neutral engine projection of one terminal session."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    root_session_id: str
    goal_sha256: str
    lifecycle: Literal["terminal"] = "terminal"
    terminal_status: Literal["stopped", "goal_met", "failed", "max_turns"]
    stop_reason: str
    terminal_state_revision: int = Field(ge=0)
    control: OutcomeArtifactRef | None = None
    handoff: OutcomeHandoff
    fallback_summary: OutcomeFallbackSummary | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    delivery_refs: list[str] = Field(default_factory=list)
    eval_refs: list[str] = Field(default_factory=list)
    trace_seal_refs: list[str] = Field(default_factory=list)
    created_at: datetime

    @field_validator("stop_reason")
    @classmethod
    def require_stop_reason(cls, value: str) -> str:
        """Require a specific terminal lifecycle reason."""

        reason = value.strip()
        if not reason:
            raise ValueError("session outcome stop_reason must not be blank")
        return reason


def _is_full_sha256(*, value: str) -> bool:
    """Return whether a value is a complete prefixed SHA-256 digest."""

    if not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        return False
    return all(character in "0123456789abcdef" for character in value[7:].lower())


class AttemptAssignment(BaseModel):
    schema_version: int = Field(default=1)
    identity: dict[str, Any]
    actor: dict[str, Any]
    objective: dict[str, Any]
    absolute_paths: dict[str, str | None]
    ownership: dict[str, str]
    provenance: dict[str, str]
    context: dict[str, Any] = Field(default_factory=dict)
