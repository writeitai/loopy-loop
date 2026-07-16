# HTTP Contract

loopy-loop exposes two coordinator endpoints: `POST /register` assigns the one
available worker to the deepest runnable session, and `POST /finished` records
one mechanical harness result before returning the next assignment. Recursive
parent/child handoffs are files, not additional HTTP endpoints.

The wire models live in `src/loopy_loop/models.py`; endpoint validation and
state transitions live in `create_coordinator_app()` and `CoordinatorService`
in `src/loopy_loop/coordinator_app.py`.

## Protocol generations

Every fresh 0.7 coordinator run creates a session tree with state schema v2.
An existing state schema v1 tree can still be resumed with its historical wire
behavior. Protocol generation is selected from durable session state, not from
a client preference:

- `/register` always requires a `worker` process identity. This requirement
  predates v2; a missing identity returns HTTP 400.
- A v1 session accepts a registration with just that identity. The new
  registration fields are optional in the Pydantic model so old persisted
  sessions remain usable.
- A v2 session requires `worker_protocol_version >= 2` and every capability
  named below. Missing support returns HTTP 426; the coordinator does not
  silently downgrade a fresh session.
- The bundled v2 worker also sends the absolute checkout path and the stable
  checkout identity from `.loopy_loop/repository.json`. The coordinator checks
  either value when supplied. This catches a worker connected from the wrong
  checkout, but is not a general filesystem sandbox.

The v2 capability set is deliberately feature-named rather than inferred from
package versions:

```text
assignment_v1
frozen_workflow_v1
trace_manifest_v1
caller_run_record_v1
coordinator_input_v1
spawn_assignment_v1
nested_caller_context_v1
```

The first three are loopy worker capabilities. The last four are advertised by
team-harness through `get_capabilities()` and forwarded by
`worker._worker_capabilities()`. This lets the coordinator reject an
installation that can run agents but cannot satisfy the selected provenance
and trace contract.

`nested_caller_context_v1` means a built-in `type=harness` spawn inherits the
same root/current session, depth, workflow role, and loopy attempt; changes to
the direct agent's absolute assignment and nested trace root; and records the
parent harness-run ID. It is another harness coordinator inside the same
workflow assignment, not another durable loop layer. Generic subprocesses are
not inferred to have this lineage.

## POST /register

The bundled 0.7 worker sends:

```json
{
  "worker": {
    "hostname": "buildbox",
    "pid": 4242,
    "starttime": "lstart:Sun Jul 12 00:00:00 2026"
  },
  "worker_protocol_version": 2,
  "capabilities": [
    "assignment_v1",
    "caller_run_record_v1",
    "coordinator_input_v1",
    "frozen_workflow_v1",
    "nested_caller_context_v1",
    "spawn_assignment_v1",
    "trace_manifest_v1"
  ],
  "repo_root": "/absolute/path/to/target-repo",
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f"
}
```

`worker.starttime` is team-harness's pid-reuse-resistant process token. It can
be null for an older identity record, in which case same-host liveness may be
unknown rather than verified. `repository_id` is a random, checkout-local
identity created once by `assignments.ensure_repository_identity()`; it is not
a git remote or commit identifier.

### Run response

A v2 run response contains the legacy scheduling fields plus protocol,
repository, immutable-snapshot, and assignment fields:

```json
{
  "action": "run",
  "workflow_set": "inner_outer_eval",
  "workflow_id": "inner",
  "session_id": "20260715_143022_71393ee22450_ab12cd34",
  "iteration": 3,
  "attempt_id": "a1b2c3d4e5f6",
  "config_snapshot": {
    "goal": "Ship a minimal working landing page",
    "goal_hash": "71393ee22450",
    "workflow_set": "inner_outer_eval",
    "completion_criteria": ["Homepage renders without errors"],
    "stop_criteria": ["A workflow publishes valid terminal control"],
    "max_turns": 20,
    "goal_check_consecutive_failures_cap": 3,
    "team_harness_provider": "openai_compat",
    "team_harness_model": "gpt-5.5",
    "team_harness_agents": ["codex"],
    "team_harness_agent_models": {"codex": "gpt-5.5"},
    "team_harness_agent_reasoning_efforts": {"codex": "high"},
    "team_harness_max_retries": null,
    "team_harness_retry_base_delay_s": null,
    "team_harness_retry_max_delay_s": null,
    "team_harness_api_base": "https://openrouter.ai/api/v1",
    "team_harness_api_key_env": "OPENROUTER_API_KEY",
    "team_harness_system_prompt_extension": ""
  },
  "stop_reason": null,
  "coordinator_protocol_version": 2,
  "required_capabilities": [
    "assignment_v1",
    "caller_run_record_v1",
    "coordinator_input_v1",
    "frozen_workflow_v1",
    "nested_caller_context_v1",
    "spawn_assignment_v1",
    "trace_manifest_v1"
  ],
  "repo_root": "/absolute/path/to/target-repo",
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f",
  "assignment_path": "/absolute/path/to/target-repo/.loopy_loop/sessions/session-id/iterations/0003_inner/workflow_snapshot/a1b2c3d4e5f6/assignment.json",
  "assignment_sha256": "sha256:<64 hex characters>",
  "workflow_snapshot": {
    "schema_version": 1,
    "session_id": "20260715_143022_71393ee22450_ab12cd34",
    "workflow_set": "inner_outer_eval",
    "workflow_id": "inner",
    "iteration": 3,
    "attempt_id": "a1b2c3d4e5f6",
    "snapshot_root": "/absolute/path/to/target-repo/.loopy_loop/sessions/session-id/iterations/0003_inner/workflow_snapshot/a1b2c3d4e5f6",
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

`config_snapshot.goal` is resolved text, not the configured goal-file path.
The snapshot freezes execution settings for the whole recursive tree; a child
changes its scoped goal, criteria, and workflow set but does not silently pick
up later model/config edits.

`workflow_snapshot` identifies the scheduler-selected workflow config, prompt,
workflow-set contract, and root execution config. The coordinator materializes
these files and hashes before dispatch, creates `assignment.json`, and freezes
its SHA-256 in the task response. The worker verifies their identity, absolute
location, manifest, hashes, and independently reconstructed assignment at the
returned absolute `assignment_path`. The assignment binds one checkout,
session layer, workflow role, iteration, and attempt and gives the harness
coordinator absolute paths for that layer. See
`assignments.materialize_workflow_snapshot()`,
`assignments.verify_workflow_snapshot()`, and
`assignments.build_attempt_assignment()`.

A v1 run response uses the same additive response model but has
`coordinator_protocol_version: 1`, an empty `required_capabilities`, no
workflow snapshot or assignment path, and follows the legacy workflow/output
path. Fields that do not apply are null.

### Stop response

When the active tree is terminal, `/register` returns the same model with no
assignment:

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
  "workflow_snapshot": null
}
```

### Registration, liveness, and recovery

If a prior `current_task` is still durable, registration follows these rules:

1. If its recorded same-host worker is verifiably alive with the same pid and
   starttime, the coordinator returns HTTP 409 and does not duplicate work.
2. Otherwise it first looks for a matching
   `pending_finished_request.json` or `result.json` in the iteration directory.
   Recoverable completion is recorded before scheduling anything new.
3. With no recoverable result, it applies the configured drain/reap policy to
   team-harness processes discoverable for that attempt. Replacement is
   refused with HTTP 409 while ownership or a remaining process cannot be
   resolved safely. A processed recovery is described by `salvage.json`.
4. Once the interrupted task is settled, it is recorded as abandoned and the
   normal stop/child/workflow scheduler advances.

Recovery runs its potentially long process handling outside the state lock,
then revalidates and commits under the lock. The bundled worker therefore uses
an unbounded read timeout only for `/register`. Process liveness/reaping is
same-host; a remote identity cannot be assumed dead.

On `--resume`, the coordinator follows durable `active_child_session_id`
pointers to the deepest session. Terminal descendants are finalized and the
tree is unwound iteratively. The scheduler never dispatches a parent and its
active child at the same time.

Relevant error statuses are:

- HTTP 400: missing worker identity.
- HTTP 409: a live owner, stale different owner, wrong checkout/identity, or
  unresolved recovery makes dispatch unsafe.
- HTTP 426: a v2 tree requires a newer worker protocol or capability.
- HTTP 503: the coordinator state lock could not be acquired in time.

## POST /finished

The bundled v2 worker posts:

```json
{
  "workflow_id": "inner",
  "session_id": "20260715_143022_71393ee22450_ab12cd34",
  "iteration": 3,
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
    "starttime": "lstart:Sun Jul 12 00:00:00 2026"
  },
  "repository_id": "repo-c50d0d9a46c843ecaa493243baed524f",
  "assignment_sha256": "sha256:<64 hex characters>",
  "harness_run_id": "run-123",
  "trace_manifest_path": "/absolute/path/to/trace_manifest.json"
}
```

The response is another `TaskResponse`: it either dispatches the next task or
stops. Posting `/finished` is therefore both acknowledgement and the next
scheduling poll.

For a task dispatched under the complete v2 worker handshake, completion must
echo the exact worker owner, repository identity, and SHA-256 of the immutable
`assignment.json`. Every modern task also has a unique `attempt_id`; a late
completion from a superseded attempt cannot complete a new retry with the same
session/workflow/iteration coordinates.

For a matching active task, the coordinator records:

- `success`, result/error, and `failure_kind` in history;
- optional coordinator-model usage and worker-measured duration in the durable
  ledger;
- the team-harness run ID, logical `trace_manifest_ref`, accepted assignment
  hash, and SHA-256 hashes of the exact `/finished` request and
  exact returned response. Only legacy history retains the worker's absolute
  `trace_manifest_path`; a crash-abandonment entry records the assignment hash
  but has no finished request/response hashes;
- any structurally valid eval/control transition produced by the workflow.

The coordinator created the canonical active trace when it dispatched this
task; the worker records the exact run `TaskResponse` there as
`protocol/task_response.json`. A supplied `trace_manifest_path` must be that
absolute session/attempt-derived manifest. Missing or different paths mark
capture incomplete and cannot redirect finalization.

`success` has the deliberately narrow D3 meaning: team-harness completed
without raising an execution error. It does not mean the work is correct and
does not consult spawned-agent exit codes for semantic acceptance. Eval and
control artifacts decide whether a session goal is met.

`usage` covers team-harness coordinator turns found in the returned
`run_json_path`; agent CLI subprocess usage is not measurable. Missing usage
means unknown, not zero. `turns_without_usage > 0` also makes the total only a
measured subtotal. `failure_kind` is `transient`, `deterministic`, or `unknown`
for worker-reported failures; coordinator crash recovery records `crash`.

Stale and replay rules are:

- A mismatch in session, workflow, iteration, or modern attempt ID does not
  mutate the live task.
- The live task can be replayed only to its recorded owner. A stale completion
  from another identified worker returns HTTP 409.
- If no task is active, `/finished` advances exactly as `/register` would.
- A stale call may receive the current scheduler response, but it never appends
  history, creates or updates a finalization intent, records a finished
  exchange, or seals the stale attempt.
- A task persisted before attempt IDs or owner binding retains only its legacy
  comparison behavior; tolerance belongs to that old task, never to a new one.

The worker writes `result.json` and `pending_finished_request.json` before the
HTTP call. It removes the pending handoff only after acknowledgement. This is
why a worker crash between local completion and `/finished` can be recovered
without rerunning the harness.

For an exact matching completion, the coordinator writes a trace-finalization
intent before committing state, then records the exact observed response and
seals. Startup acts only when history proves that completion or abandonment
committed; an interrupted response is marked unavailable rather than invented.
Trace failure is logged and does not roll back semantic state. The full
artifact ordering and repair rules are in
[`session-layout.md`](./session-layout.md#caller-owned-attempt-traces).

## File protocols consumed during `/finished`

The following are not HTTP bodies. They are durable workflow outputs that the
coordinator reads while recording a matching completion. The assignment
contains their absolute paths; portable cross-file links use logical
`session:/`, `parent:/`, `root:/`, or `trace:` references.

### Recursive child request v2

A workflow requests a depth-first child by atomically publishing a unique JSON
file under its assigned `child_requests/pending/` directory:

```json
{
  "schema_version": 2,
  "request_id": "feature-auth-1",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "a1b2c3d4e5f6",
    "parent_work_item_id": "FEATURE-4",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Implement the selected authentication slice.",
    "completion_criteria": ["The child-scoped outcome passes evaluation"],
    "stop_criteria": ["A genuinely terminal blocker is established"],
    "constraints": [],
    "deliverables": ["code and verification evidence"],
    "required_evidence": ["eval, git, and delivery receipts"]
  },
  "inputs": [
    {
      "ref": "session:/project_state/work-items/FEATURE-4.json",
      "sha256": "sha256:<64 hex characters>"
    }
  ]
}
```

The coordinator checks the schema, requested workflow set, input reference
confinement, and input hashes. A dispatchable body is copied unchanged to
`child_requests/accepted/<request_id>.json`, hashed, indexed in
`children.json`, and copied again into the child's immutable
`inputs/accepted_request.json`. Every declared parent `inputs[]` reference is
resolved and hash-checked from the parent's scope, then its exact bytes are
copied to the child's `inputs/artifacts/`. The child goal contract and all of
its attempts use only those child-local references, hashes, and worker-local
absolute paths; `session.json.origin` retains the parent source-to-copy mapping
for provenance. Later parent edits therefore cannot change or wedge an accepted
child assignment. The pending file is then removed. `request_id` supplies
idempotency across retries; it cannot be reused with a different accepted body.

An invalid or undispatchable v2 pending request is moved to
`child_requests/rejected/` and gets a separate rejection receipt containing
the reason and original hash. This is a terminal disposition of that request,
not a human approval gate. A later workflow may autonomously publish a repaired
request with a new identity.

### Eval receipt and goal-check projection

The canonical evaluation artifact is a compact receipt under
`eval_receipts/`. Its own schema is currently version 1 even when the enclosing
session protocol is v2:

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
    "iteration": 4,
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
  "judge": {"provider": "codex", "model": "gpt-5.5", "reasoning_effort": "high"},
  "check_results": [
    {"check_id": "goal-outcome", "passed": true, "reason": "Evidence passes."}
  ],
  "verdict": {"goal_met": true, "reason": "All declared checks passed."},
  "canonical_report_ref": "session:/eval_receipts/eval-a1b2c3d4e5f6.report.md",
  "canonical_report_sha256": "sha256:<64 hex characters>",
  "raw_report_refs": [
    "trace:trace-fedcba654321:/eval/report.json"
  ],
  "raw_report_sha256s": {
    "trace:trace-fedcba654321:/eval/report.json": "sha256:<64 hex characters>"
  },
  "created_at": "2026-07-15T12:00:00Z"
}
```

An eval-emitting workflow also writes a small iteration-local
`goal_check.json` whose verdict and reason exactly project that receipt:

```json
{
  "schema_version": 2,
  "goal_met": true,
  "reason": "All declared checks passed.",
  "eval_receipt_ref": "session:/eval_receipts/eval-a1b2c3d4e5f6.json"
}
```

Every receipt, passing or failing, must have exactly one `raw_report_refs`
entry: the producing attempt's canonical `eval/report.json`.
`raw_report_sha256s` has that same single key, and both the canonical and raw
artifacts must match their full SHA-256 values.
`CoordinatorService._read_goal_check_signal()` and
`_validate_eval_receipt_artifacts()` also check the exact current
producer/attempt/iteration, root/session/goal identity, every regular
`*.yaml`/`*.yml` check discovered recursively below `eval_checks/`,
definition-byte hashes, and agreement between receipt and projection. Symlinks
and non-files are rejected. Receipt JSON/schema failures retain up to eight
field-qualified validation messages in failed history and terminal-control
rejection evidence so the responsible workflow can repair the exact field. A
passing report must additionally record the exact absolute target
`project_root`, the producing attempt's absolute canonical `output_dir`,
`run_passed: true`, `pass_threshold: 1.0`, exactly the receipt's check IDs with
every status `passed` and exit code zero, and per-check
`details.agent_type`/`details.model`/`details.reasoning_effort` matching the
receipt's judge. At terminal acceptance the coordinator recaptures live Git
state and requires its HEAD and `loopy-git-status-diff-v1-sha256` digest to
match the receipt. The digest binds filtered porcelain records plus Git's
staged and unstaged binary diffs. It intentionally relies on Git's observable
boundary rather than independently parsing and re-hashing the complete index.

These checks establish provenance and all-pass mechanics. The coordinator does
not re-evaluate the LLM judge's semantic reasons or require stock deterministic
checks (D3/D4/D8).

A valid `goal_check.json` is not a stop switch.

### Terminal control v2

A packaged v2 workflow set accepts `goal_met` only from its declared
`goal_control_role` and only when it cites the matching passing eval receipt:

```json
{
  "schema_version": 2,
  "control_id": "control-fedcba654321",
  "state": "stopped",
  "reason": "The session-scoped eval passed.",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "current-session-id",
    "workflow_id": "eval_runner",
    "attempt_id": "fedcba654321"
  },
  "eval_receipt_ref": "session:/eval_receipts/eval-a1b2c3d4e5f6.json",
  "attempted_routes": [],
  "evidence_refs": [],
  "created_at": "2026-07-15T12:00:00Z"
}
```

The D5 last-resort `unresolvable_error` form instead requires a producer role
listed by the workflow contract, at least one attempted autonomous route, and
no eval receipt. It may include logical evidence references.

Both terminal forms must identify the exact current session/workflow/attempt
whose matching `/finished` is being recorded. Control written by an earlier
attempt, another layer, or a spawned agent is rejected. A spawned agent reports
to its harness coordinator; the current accountable workflow publishes the
session-owned record.

Invalid terminal v2 control is not accepted as a stop. The coordinator moves
the original to `control_rejected/`, writes a reasoned record in
`protocol_failures/`, restores a running control record, and lets later work
repair the protocol. Repeated invalid control is bounded by
`goal_check_consecutive_failures_cap`, which is currently shared by the eval
projection and terminal-control protocol. There is no
paused/waiting-for-human control state.

## Legacy compatibility boundary

Resume keeps old work usable without weakening new work:

- Missing `LoopState.schema_version` means v1. Fresh state explicitly writes
  v2 and a revision.
- A v1 session may use legacy child requests shaped as
  `{"schema_version":1,"workflow_set":"...","goal":"..."}` in the flat
  `child_requests/` directory, terminal control v1, and goal-check v1.
- The child-request reader observes both the legacy flat inbox and v2
  `pending/`. Flat rejected requests retain the historical `.rejected` naming;
  new pending requests use the `rejected/` archive.
- A v1 task has no immutable workflow snapshot/assignment requirement and may
  use `harness_outputs/` rather than the caller-owned trace path.
- A packaged v2 workflow contract does not accept v1 terminal control,
  goal-check output, or child requests. Such artifacts are repairable protocol
  failures, not implicit downgrades.

Compatibility code is concentrated in
`CoordinatorService._workflow_contract_for_state()`, the dual child-request
reader in `_dispatch_child_session_if_requested()`, and the optional/additive
fields in `models.py`.
