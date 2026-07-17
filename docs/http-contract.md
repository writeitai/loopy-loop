# HTTP Contract

loopy-loop has two coordinator endpoints:

- `POST /register` gives the single worker the deepest runnable assignment;
- `POST /finished` records one mechanical harness result and returns the next
  assignment or stop response.

Recursive parent/child handoffs, semantic progress, evaluation evidence, and
terminal decisions are files, not additional HTTP endpoints. Wire models live
in `src/loopy_loop/models.py`; endpoint checks and transitions live in
`create_coordinator_app()` and `CoordinatorService` in
`src/loopy_loop/coordinator_app.py`.

## Version and capability negotiation

The agent-facing protocol is frozen by each session's
`workflow_contract.json` and engine-owned contract copy. It is not selected by
the worker. Fresh stock `inner_outer_eval` and `pm_planner_dispatcher` sessions
declare protocol v3. The persisted coordinator-state schema remains v2; those
are separate version axes.

`POST /register` always requires a process identity. For a protocol-v3 tree it
also requires:

- `worker_protocol_version >= 3`;
- the absolute checkout path and stable checkout-local repository ID; and
- every feature capability below.

```text
# carried forward from protocol v2
assignment_v1
frozen_workflow_v1
trace_manifest_v1
caller_run_record_v1
coordinator_input_v1
spawn_assignment_v1
nested_caller_context_v1

# added for protocol v3
assignment_v2
harness_capability_roster_v1
orchestrator_control_v3
scheduler_view_v1
semantic_handoff_v1
capability_roster_context_v1
```

The first group combines loopy worker features with team-harness caller/trace
features. The v3 additions mean that the worker understands the semantic path
map, tree-wide harness roster, scheduler view, orchestrator-owned control, and
rolling handoff. `capability_roster_context_v1` is advertised by team-harness:
it means the worker can forward the root roster's absolute path, SHA-256, and
compact summary through caller context so a built-in nested `type=harness`
coordinator inherits the same catalog.

`nested_caller_context_v1` is narrower: it preserves root/current session,
depth, workflow role, parent loopy attempt, direct-agent assignment, nested
trace root, and parent harness-run identity. A nested harness remains a dynamic
delegate in the current durable assignment; it is not a child Loopy session.

Missing version or capability support returns HTTP 426 before a v3 assignment
is dispatched. The coordinator never silently downgrades the session. A wrong
checkout path or repository identity returns HTTP 409.

## `POST /register`

The bundled v3 worker sends:

```json
{
  "worker": {
    "hostname": "buildbox",
    "pid": 4242,
    "starttime": "lstart:Fri Jul 17 10:00:00 2026"
  },
  "worker_protocol_version": 3,
  "capabilities": [
    "assignment_v1",
    "assignment_v2",
    "caller_run_record_v1",
    "capability_roster_context_v1",
    "coordinator_input_v1",
    "frozen_workflow_v1",
    "harness_capability_roster_v1",
    "nested_caller_context_v1",
    "orchestrator_control_v3",
    "scheduler_view_v1",
    "semantic_handoff_v1",
    "spawn_assignment_v1",
    "trace_manifest_v1"
  ],
  "repo_root": "/absolute/path/to/target-repo",
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f"
}
```

`worker.starttime` is team-harness's pid-reuse-resistant process token. It may
be null for an older identity record, in which case same-host liveness can be
unknown. `repository_id` is a random identity created once in
`.loopy_loop/repository.json`; it is not a Git remote or commit.

### Run response

A v3 run response includes scheduling identity, the tree-frozen execution
snapshot, required capabilities, repository binding, and exact attempt
snapshot/assignment coordinates:

```json
{
  "action": "run",
  "workflow_set": "inner_outer_eval",
  "workflow_id": "outer",
  "session_id": "20260717_100000_71393ee22450_ab12cd34",
  "iteration": 4,
  "attempt_id": "a1b2c3d4e5f6",
  "config_snapshot": {
    "goal": "Deliver the scoped outcome",
    "goal_hash": "71393ee22450",
    "workflow_set": "inner_outer_eval",
    "completion_criteria": ["The declared outcome is evidenced"],
    "stop_criteria": ["A valid terminal control is accepted"],
    "max_turns": 40,
    "goal_check_consecutive_failures_cap": 3,
    "team_harness_provider": "<configured provider>",
    "team_harness_model": "<configured coordinator model>",
    "team_harness_agents": ["<enabled family>"],
    "team_harness_agent_models": {},
    "team_harness_agent_reasoning_efforts": {},
    "team_harness_system_prompt_extension": "<frozen roster guidance>"
  },
  "stop_reason": null,
  "coordinator_protocol_version": 3,
  "required_capabilities": [
    "assignment_v1",
    "assignment_v2",
    "caller_run_record_v1",
    "capability_roster_context_v1",
    "coordinator_input_v1",
    "frozen_workflow_v1",
    "harness_capability_roster_v1",
    "nested_caller_context_v1",
    "orchestrator_control_v3",
    "scheduler_view_v1",
    "semantic_handoff_v1",
    "spawn_assignment_v1",
    "trace_manifest_v1"
  ],
  "repo_root": "/absolute/path/to/target-repo",
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f",
  "assignment_path": "/absolute/path/to/target-repo/.loopy_loop/sessions/<session>/iterations/0004_outer/workflow_snapshot/a1b2c3d4e5f6/assignment.json",
  "assignment_sha256": "sha256:<64 hex characters>",
  "workflow_snapshot": {
    "schema_version": 1,
    "session_id": "20260717_100000_71393ee22450_ab12cd34",
    "workflow_set": "inner_outer_eval",
    "workflow_id": "outer",
    "iteration": 4,
    "attempt_id": "a1b2c3d4e5f6",
    "snapshot_root": "/absolute/path/to/workflow_snapshot/a1b2c3d4e5f6",
    "workflow_config_path": "/absolute/path/to/workflow_snapshot/config.yaml",
    "workflow_prompt_path": "/absolute/path/to/workflow_snapshot/prompt.txt",
    "workflow_contract_path": "/absolute/path/to/workflow_snapshot/workflow_contract.yaml",
    "root_config_snapshot_path": "/absolute/path/to/workflow_snapshot/root_config_snapshot.json",
    "workflow_config_sha256": "sha256:<64 hex characters>",
    "workflow_prompt_sha256": "sha256:<64 hex characters>",
    "workflow_contract_sha256": "sha256:<64 hex characters>",
    "root_config_snapshot_sha256": "sha256:<64 hex characters>"
  }
}
```

The actual `config_snapshot` also carries the remaining frozen retry/provider
fields defined by `RootConfigSnapshot`; they are omitted from this example for
readability. The snapshot freezes execution settings for the entire recursive
tree. A child changes its scoped goal, criteria, and selected workflow set but
does not silently pick up later configuration edits.

Before returning the response, the coordinator:

1. freezes the selected workflow config, prompt, workflow contract, and root
   execution snapshot;
2. writes a conditional `scheduler_view.json` for this attempt;
3. builds schema-v2 `assignment.json` with actor/objective identity, the full
   absolute semantic path map, compact workflow/scheduler/capability context,
   and provenance hashes; and
4. freezes the assignment SHA-256 in the current task and response.

The worker verifies the immutable files, locations, manifest, hashes, and an
independently reconstructed assignment before invoking team-harness. See
`assignments.materialize_workflow_snapshot()`,
`assignments.build_attempt_assignment()`, and
`assignments.verify_workflow_snapshot()`.

The assignment paths include the canonical layer goal/contract/inputs,
plan/tasks/current state/decisions/finished ledger/eval state/handoff, engine
state, terminal outcome, workflow contract/roster, scheduler view, root
capability roster, user inputs, recursive request/outcome/acceptance records,
Git and delivery receipts, session control, attempt root, and trace root.
Parent-only origin paths are explicit nulls at the root. See
[`session-layout.md`](./session-layout.md#attempt-snapshot-and-absolute-assignment-paths)
for the stable key list.

### Stop response

When the active tree is terminal, the same response model has no assignment:

```json
{
  "action": "stop",
  "workflow_set": null,
  "workflow_id": null,
  "session_id": null,
  "iteration": null,
  "attempt_id": null,
  "config_snapshot": null,
  "stop_reason": "goal_met",
  "coordinator_protocol_version": null,
  "required_capabilities": [],
  "repo_root": null,
  "repository_id": null,
  "assignment_path": null,
  "assignment_sha256": null,
  "workflow_snapshot": null
}
```

### Registration, liveness, and recovery

If `state.json` still contains a current task, registration proceeds as
follows:

1. A verifiably live same-host owner with matching PID/starttime receives HTTP
   409; work is not duplicated.
2. Otherwise, the coordinator looks for that attempt's matching
   `pending_finished_request.json` or `result.json` and records a recoverable
   completion before scheduling new work.
3. Without a result, it applies the configured drain/reap policy to tracked
   team-harness processes. Replacement remains HTTP 409 while ownership or a
   possibly live process is unresolved. A completed recovery is described by
   `salvage.json`.
4. After safe abandonment, normal terminal/child/workflow scheduling resumes.

Potentially long process handling occurs outside the state lock, then the
coordinator revalidates under the lock. On resume it follows durable
`active_child_session_id` pointers to the deepest session and iteratively
unwinds terminal descendants. A parent and its active child are never
dispatched concurrently.

Relevant statuses are:

- HTTP 400: required worker identity is absent;
- HTTP 409: live/stale ownership, wrong checkout, or unresolved recovery makes
  dispatch unsafe;
- HTTP 426: protocol version or a required capability is missing; and
- HTTP 503: the coordinator state lock could not be acquired in time.

## `POST /finished`

The bundled worker posts one exact completion envelope:

```json
{
  "workflow_id": "outer",
  "session_id": "20260717_100000_71393ee22450_ab12cd34",
  "iteration": 4,
  "attempt_id": "a1b2c3d4e5f6",
  "success": true,
  "text": "done",
  "error": null,
  "failure_kind": null,
  "usage": {
    "prompt_tokens": 5210,
    "completion_tokens": 902,
    "turns": 3,
    "turns_without_usage": 0
  },
  "duration_s": 187.4,
  "worker": {
    "hostname": "buildbox",
    "pid": 4242,
    "starttime": "lstart:Fri Jul 17 10:00:00 2026"
  },
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f",
  "assignment_sha256": "sha256:<64 hex characters>",
  "harness_run_id": "run-123",
  "trace_manifest_path": "/absolute/path/to/trace_manifest.json",
  "trace_incomplete": false,
  "trace_error": null
}
```

The response is another `TaskResponse`, so `/finished` is both completion
acknowledgement and the next scheduling poll.

For protocol v3, completion must echo the exact worker owner, repository ID,
assignment SHA-256, session, workflow, iteration, and attempt ID. A late result
from a superseded attempt cannot complete a retry with otherwise identical
coordinates.

The coordinator records the mechanical result, optional usage/duration,
harness run, relocatable trace reference, assignment hash, and hashes of the
exact request and returned response. The worker-written absolute
`trace_manifest_path` must equal the canonical session/attempt-derived trace;
it cannot redirect finalization.

`success: true` has the narrow D3 meaning: team-harness returned without an
execution error. It does not assert that implementation work is correct,
accepted, merged, or complete, and it does not infer semantic failure from a
spawned process exit code. In protocol v3, the contract's durable orchestrator
decides `goal_met` through `control.json` after weighing optional eval and other
evidence.

For protocol v3, `_record_finished_task()` also:

- scans current-attempt receipts when the workflow is a declared check runner,
  accepting valid provenance into compact seals and emitting diagnostics for
  invalid observations without changing mechanical success; and
- observes the rolling handoff's identity/revision/hash, again as diagnostics
  rather than a semantic gate.

Stale/replay rules are strict: a coordinate or owner mismatch does not mutate
the live task, and a stale call cannot append history, create a trace
finalization intent, or seal the stale attempt. If no task is active,
`/finished` advances as `/register` would.

The worker writes `result.json` and `pending_finished_request.json` before the
HTTP call, removing the latter only after acknowledgement. This closes the
worker-crash window between local completion and coordinator acceptance.

## File protocols consumed during `/finished`

These records are not HTTP bodies. Workflows publish them at assignment-provided
absolute paths. Durable links use confined logical references such as
`session:/`, `root:/`, `parent:/`, `session:<id>:/`, and `trace:`.

### Recursive child request

Protocol-v3 recursive sessions continue to use child-request schema v2:

```json
{
  "schema_version": 2,
  "request_id": "foundation-phase",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "a1b2c3d4e5f6",
    "parent_work_item_id": "PHASE-0",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Make the development foundations ready and evidenced.",
    "completion_criteria": ["The phase outcome is complete and reviewable"],
    "stop_criteria": ["A genuinely terminal blocker is established"],
    "constraints": [],
    "deliverables": ["implementation and verification evidence"],
    "required_evidence": ["appropriate Git, review, test, and delivery refs"]
  },
  "inputs": [
    {
      "ref": "session:/project_state/dispatch_inputs/foundation-phase.json",
      "sha256": "sha256:<64 hex characters>"
    }
  ]
}
```

The coordinator validates the schema, current origin attempt, requested
workflow set, reference confinement, and hashes. It copies a valid body into
`child_requests/accepted/`, binds it into `children.json`, and freezes exact
request/input bytes in the child's own `inputs/` and `goal_contract.json`.
`request_id` makes dispatch idempotent and cannot be reused with contradictory
bytes.

Invalid requests move to `child_requests/rejected/` with their original hash
and reason. This is a repairable autonomous disposition, not a human approval
step. The stock PM dispatcher publishes one planner-selected high-level
milestone outcome; it does not pre-decompose the child's leaf plan.

### Advisory eval receipt and acceptance seal

The canonical receipt schema remains v1 inside a protocol-v3 session. It binds
the evaluated root/session/goal/Git subject, exact producing
workflow/iteration/attempt/harness run, check definitions, judge settings,
per-check results, verdict, canonical report, raw report references/hashes, and
creation time. A compact example is:

```json
{
  "schema_version": 1,
  "eval_id": "eval-a1b2c3d4e5f6",
  "subject": {
    "root_session_id": "root-session-id",
    "session_id": "current-session-id",
    "goal_hash": "sha256:<64 hex characters>",
    "git_commit": "0123456789abcdef",
    "dirty_tree_digest": "sha256:<64 hex characters>"
  },
  "producer": {
    "workflow_id": "eval_runner",
    "iteration": 3,
    "attempt_id": "fedcba654321",
    "harness_run_id": "run-456"
  },
  "checks": [
    {
      "check_id": "goal-outcome",
      "definition_sha256": "sha256:<64 hex characters>",
      "kind": "harness_judge"
    }
  ],
  "judge": {
    "provider": "<judge family>",
    "model": "<judge model>",
    "reasoning_effort": "<effective effort>"
  },
  "check_results": [
    {"check_id": "goal-outcome", "passed": false, "reason": "Gap found."}
  ],
  "verdict": {"goal_met": false, "reason": "The gap remains."},
  "canonical_report_ref": "session:/eval_receipts/eval-a1b2c3d4e5f6.report.md",
  "canonical_report_sha256": "sha256:<64 hex characters>",
  "raw_report_refs": ["trace:trace-fedcba654321:/eval/report.json"],
  "raw_report_sha256s": {
    "trace:trace-fedcba654321:/eval/report.json": "sha256:<64 hex characters>"
  },
  "created_at": "2026-07-17T10:00:00Z"
}
```

When the exact producing attempt completes, the coordinator accepts a receipt
only if its producer role is in the frozen `evaluation.check_runner_roles` and
all subject, harness, check-definition, report, judge, hash, and evaluated-Git
provenance validates. It then writes an engine sidecar:

```json
{
  "schema_version": 1,
  "receipt_ref": "session:/eval_receipts/eval-a1b2c3d4e5f6.json",
  "receipt_sha256": "sha256:<64 hex characters>",
  "subject": {"root_session_id": "root-session-id", "session_id": "current-session-id", "goal_hash": "sha256:<64 hex characters>", "git_commit": "0123456789abcdef", "dirty_tree_digest": "sha256:<64 hex characters>"},
  "producer": {"workflow_id": "eval_runner", "iteration": 3, "attempt_id": "fedcba654321", "harness_run_id": "run-456"},
  "evaluated_git": {"git_commit": "0123456789abcdef", "dirty_tree_digest": "sha256:<64 hex characters>"},
  "accepted_at": "2026-07-17T10:01:00Z"
}
```

The sidecar lives at `eval_receipts/accepted/<eval_id>.json`; its trust-root
copy lives in `state.json`. A later orchestrator attempt can cite the receipt
without requiring gitignored raw trace bytes to remain present. The receipt's
verdict may pass or fail: acceptance establishes provenance, not semantic
agreement.

Missing, malformed, or non-passing advisory eval output is recorded in an
`eval_observation` event. It does not flip `IterationResult.success`, consume a
generic workflow failure budget, create `goal_check_broken`, or block
orchestrator completion. `goal_check.json` is optional/legacy in v3.

### Protocol-v3 terminal control

Successful control comes from the frozen orchestration owner (`outer` in
`inner_outer_eval`, `planner` in `pm_planner_dispatcher`):

```json
{
  "schema_version": 3,
  "control_id": "control-a1b2c3d4e5f6",
  "state": "stopped",
  "reason": "Why this layer's own goal is complete.",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "current-session-id",
    "workflow_id": "outer",
    "attempt_id": "a1b2c3d4e5f6"
  },
  "evidence_refs": ["session:/delivery_receipts/pr-42.json"],
  "eval_receipt_refs": [
    "session:/eval_receipts/eval-a1b2c3d4e5f6.json"
  ],
  "handoff_ref": "session:/project_state/handoff.json",
  "created_at": "2026-07-17T10:02:00Z"
}
```

`evidence_refs`, `eval_receipt_refs`, and `handoff_ref` are optional; lists may
be empty. The engine requires exact current producer identity and completion
authority. Any cited evidence must resolve to a file. Any cited eval receipt
must match an engine acceptance seal, this session/root/goal, and a declared
runner role. A cited handoff must be the canonical layer handoff with matching
session/goal and declared owner when it has a producer.

No eval receipt, passing verdict, same-attempt eval, or `goal_check.json` is
required. The orchestrator owns the semantic disposition, including how it
weighs conflicting observations.

The D5 last-resort blocker keeps exact identity but does not cite eval or
handoff records:

```json
{
  "schema_version": 3,
  "control_id": "control-terminal-blocker-a1b2c3d4e5f6",
  "state": "stopped",
  "reason": "The specific unavoidable blocker and why a human is required.",
  "stop_reason": "unresolvable_error",
  "producer": {
    "session_id": "current-session-id",
    "workflow_id": "inner",
    "attempt_id": "a1b2c3d4e5f6"
  },
  "attempted_routes": ["The autonomous recovery route already tried"],
  "evidence_refs": ["session:/project_state/decisions/blocker.md"],
  "created_at": "2026-07-17T10:02:00Z"
}
```

The producer must be in `terminal_blocker_reporting_roles`; attempted routes
must be non-empty. There is no `paused` or `waiting_for_human` state.

Invalid v3 terminal control is not accepted as a stop. The original bytes move
to `control_rejected/`, `protocol_failures/` records its hash, producer, and
reasons, and `control.json` becomes a running repair placeholder. Consecutive
invalid control records are bounded by the configured control-failure counter.
An invalid cited receipt is a false control claim; an uncited malformed
advisory observation is only an eval diagnostic.

### `session_outcome.json` and child link

After any v3 terminal transition, the coordinator writes the same engine-owned
result shape for root and child sessions. Control-owned outcomes include the
accepted control hash; engine-owned lifecycle stops use `control: null`:

```json
{
  "schema_version": 1,
  "session_id": "current-session-id",
  "root_session_id": "root-session-id",
  "goal_sha256": "sha256:<64 hex characters>",
  "lifecycle": "terminal",
  "terminal_status": "goal_met",
  "stop_reason": "goal_met",
  "terminal_state_revision": 13,
  "control": {
    "ref": "session:/control.json",
    "sha256": "sha256:<64 hex characters>"
  },
  "handoff": {
    "status": "valid",
    "ref": "session:/project_state/handoff.json",
    "sha256": "sha256:<64 hex characters>",
    "revision": 7
  },
  "fallback_summary": null,
  "evidence_refs": [],
  "delivery_refs": [],
  "eval_refs": [],
  "trace_seal_refs": [],
  "created_at": "2026-07-17T10:02:01Z"
}
```

Handoff status may also be `missing`, `invalid`, or `non_monotonic`. In those
cases `fallback_summary` copies the authenticated control reason, or the
factual engine stop reason when `control` is null; it does not invent a
semantic summary. A weak handoff is completeness information, not a control
veto. Delivery evidence is projected across the whole session history, not
only the terminal attempt.

The terminal status, revision, timestamp, accepted control bytes, and accepted
handoff bytes are frozen in engine-owned `state.json`. Regeneration restores
those exact bytes rather than trusting later file edits. Only the list of
trace-seal references may expand as already-accepted attempts finish trace
finalization.

A v3 parent's `child_outcomes/<request_id>.json` is a small link:

```json
{
  "schema_version": 2,
  "request_id": "foundation-phase",
  "child_session_id": "child-session-id",
  "session_outcome_ref": "session:child-session-id:/session_outcome.json",
  "session_outcome_sha256": "sha256:<64 hex characters>"
}
```

The parent independently decides acceptance, rework, or reroute. A child
outcome never sets ancestor `goal_met`.

## Frozen v1/v2 compatibility

The current implementation still resumes historical sessions under their
frozen behavior:

- v1 may register with process identity alone and may use flat child requests,
  terminal control v1, goal-check v1, and legacy output paths;
- v2 requires worker protocol 2 plus the seven carried-forward capabilities,
  repository binding, immutable assignment/snapshot, identity-bound terminal
  control, and its historical same-attempt passing receipt plus matching
  `goal_check.json` for `goal_met`; and
- v2 control uses singular `eval_receipt_ref`, while v3 uses plural
  `eval_receipt_refs`.

An explicit custom contract omitting `session_protocol_version` remains pinned
to v2; a workflow set with no contract derives conservative v1. Compatibility
is selected from durable session state, never by accepting a lower worker
version for a v3 tree.
