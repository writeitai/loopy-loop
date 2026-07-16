# Session Layout

loopy-loop separates compact durable state from detailed execution traces:

- `.loopy_loop/sessions/` is the state machine and compact evidence needed to
  schedule, recover, evaluate, and understand a recursive run.
- `.loopy_loop/traces/` is detailed, attempt-scoped observability: model
  envelopes, team-harness records, direct-agent assignments and logs, raw eval
  output, and verbose git evidence. Traces are independently sealable and
  gitignored.

Both trees are runtime output and are ignored by the generated `.gitignore`.
Ignoring session state does not make it disposable while a run is active:
continuity lives in these files. Trace retention is independent because the
compact receipts needed for correctness remain in the session.

Path construction is centralized in `src/loopy_loop/sessions.py`. Fresh 0.7
runs use the v2 layout below. Existing v1 sessions remain readable and are
described at the end.

## Recursive tree

A root session is a direct child of `.loopy_loop/sessions/`. Every requested
child is physically nested under its parent. The same shape can recurse to any
depth; there is no separate hard-coded state machine for a planner/dispatcher
or a three-loop deployment.

```text
.loopy_loop/
├── repository.json
├── sessions/
│   └── <root_session_id>/
│       ├── goal.md
│       ├── goal_contract.json
│       ├── session.json
│       ├── workflow_contract.json
│       ├── state.json
│       ├── events.jsonl
│       ├── control.json
│       ├── inputs/
│       │   ├── user_updates.jsonl
│       │   ├── accepted_request.json       # child sessions only
│       │   └── artifacts/                  # frozen parent inputs
│       ├── project_state/
│       │   └── finished.md
│       ├── eval_checks/
│       ├── eval_readiness/
│       ├── eval_receipts/
│       ├── child_requests/
│       │   ├── pending/
│       │   ├── accepted/
│       │   └── rejected/
│       ├── children.json
│       ├── child_outcomes/
│       ├── parent_acceptance/
│       ├── git_receipts/
│       ├── delivery_receipts/
│       ├── trace_seals/
│       ├── control_rejected/
│       ├── protocol_failures/
│       ├── iterations/
│       │   └── 0001_<workflow_id>/
│       ├── harness_outputs/
│       └── children/
│           └── <child_session_id>/
│               ├── parent.json
│               ├── goal.md
│               ├── goal_contract.json
│               ├── session.json
│               ├── workflow_contract.json
│               ├── state.json
│               ├── ...same session directories...
│               └── children/
│                   └── <grandchild_session_id>/...
├── traces/
│   └── <root_session_id>/sessions/<session_id>/attempts/<attempt_id>/
└── trace_finalization_outbox/
```

Only the deepest live session receives work. While a child runs, its parent has
`active_child_session_id` set and is suspended. When a child becomes terminal,
the coordinator writes a factual outcome, clears the pointer, and resumes the
parent; multiple terminal ancestors can be unwound in one iterative transition.
The durable pointer and recovery logic are in
`CoordinatorService._suspended_parent_response()` and
`_resume_parent_if_active_child_completed()`.

`.loopy_loop/repository.json` is not session state. It contains the stable
checkout identity created by `assignments.ensure_repository_identity()` and is
used to bind coordinator and worker to the same checkout.

## Immutable identity and contracts

These files explain what one loop layer is and what it owns. They are written
when the session is created and are not a substitute for mutable progress
state.

### `goal.md`

The exact resolved goal text for this layer. A child receives its
child-assignment goal, not the parent's broader goal. The configured root goal
file may later change; active work continues to use this session-local copy.

### `goal_contract.json`

The typed scope for this layer: goal and full hash, completion/stop criteria,
constraints, deliverables, required evidence, and terminal-blocker policy. A
child contract additionally freezes:

- the originating `request_id`;
- a child-local logical reference and hash of the accepted request body; and
- child-local logical references and hashes for exact copies of every declared
  parent input.

`assignments.build_attempt_assignment()` verifies these hashes again before
the harness runs and exposes worker-local absolute paths for accepted inputs.
`session.json.origin` keeps the original parent references/hashes and their
mapping to these frozen copies, so provenance does not depend on mutable parent
state.

### `session.json`

The immutable session manifest. V2 records include `session_id`,
`root_session_id`, `parent_session_id`, `depth`, `workflow_set`, `layer_kind`,
goal and goal-contract hashes, origin provenance, and creation time. A child
also has `parent.json`, which records the parent identity and physical relative
path. `references.LogicalReferenceResolver` validates physical nesting against
these declarations before resolving cross-layer references.

Session IDs use `<UTC date>_<UTC time>_<short goal hash>_<random>`. The short
fragment makes names readable; the contracts use full SHA-256 where provenance
requires it.

### `workflow_contract.json`

The selected workflow set's frozen role contract: session protocol version,
layer kind, named role responsibilities, state declarations, eval author/
runner/control roles, task-acceptance role, terminal-blocker reporting roles,
and child interface. It tells an assignment which workflow role it is playing
and prevents a later edit to `contract.yaml` from changing an active session.

## Coordinator state and observations

### `state.json`

The coordinator-owned source of truth. V2 state is revisioned on mutations and
contains:

- this layer's status, goal/workflow identity, root/parent/depth, turn limits,
  and frozen execution snapshot;
- `current_task`, including attempt, worker owner, repository identity, and
  frozen workflow-snapshot descriptor;
- iteration history and per-workflow failure counters;
- the active-child pointer and child-request/work-item provenance;
- token/duration totals and stop/control-failure state.

The convenient phase (`ready`, `executing`, `suspended`, or `terminal`) is
derived from status, current task, and active child; it is not a second durable
state machine. `StateStore` owns state writes. Agents receive `state.json` as
read-only engine state through `assignment.json`; they communicate desired
transitions through typed workflow-owned files.

### `events.jsonl`

A best-effort observability projection appended after state mutations commit.
Each line is a schema-v1 envelope with `event_id`, UTC timestamp, session ID,
type, and payload. Current event types include `session_started`,
`task_dispatched`, `task_finished`, `iteration_abandoned`, `goal_check`,
`child_started`, `child_finished`, and `session_stopped`.

Consumers must tolerate gaps, duplicates, and a truncated final line and
deduplicate by `event_id`. Scheduling and recovery use `state.json`, not the
event stream. `loopy events --follow` tails it.

### `control.json`

The workflow-owned stop request. A fresh v2 session begins with a neutral,
v1-compatible running record:

```json
{
  "state": "running",
  "reason": "session active",
  "stop_reason": null,
  "schema_version": 1
}
```

For a workflow contract with `session_protocol_version: 2`, terminal control
must use the identity-bound v2 form. Successful `goal_met` must be authored by
the declared goal-control role and cite a valid passing receipt in this
session. Last-resort `unresolvable_error` must come from an allowed role and
record autonomous routes already exhausted. See
`CoordinatorService._apply_session_control()` and `_validate_v2_control()`.

Both terminal forms must match the session, workflow, and attempt of the exact
current task whose `/finished` transition is being processed. Control from a
prior attempt or another layer is rejected even when its role name is allowed.
A spawned agent reports a conclusion to its harness coordinator; that current
workflow role publishes the session-owned control record.

Invalid terminal v2 control is moved unchanged to `control_rejected/`. A
`protocol_failures/` record preserves its hash, producer, and rejection reasons,
and `control.json` is restored to running so autonomous repair can continue.
Consecutive invalid controls use `goal_check_consecutive_failures_cap`, shared
with invalid goal-check projections. This is detection and repair, not
preventive write fencing or a human gate.

### `protocol_failures/`

Compact, durable protocol diagnoses. In addition to invalid terminal control,
the coordinator uses this directory to preserve an unreadable v2
`children.json` and record bounded reconstruction. Reconstruction derives only
from accepted requests, child manifests, and child state; it does not silently
treat a damaged ledger as empty.

## Append-only inputs

### `inputs/user_updates.jsonl`

The v2 input channel used by `loopy update`. Each operator update appends a
`user_input` record with a unique ID, target scope/session, routed destination,
timestamp, exact text, and pending acknowledgement state. Without
`--session`, the CLI routes it to the deepest active layer; an explicit session
targets that layer.

Before an attempt is rendered, `worker._semantic_prompt_context()` reads the
journal, appends one `input_delivery` record per pending input/attempt, and
includes pending records in the prompt. The workflow is instructed to append a
`user_input_acknowledgement` with disposition after acting. Earlier lines are
never edited. This distinguishes input creation, delivery, and semantic
acknowledgement without pretending the coordinator understood the text.

`updates_from_user.md` is still scaffolded/readable for legacy compatibility,
but new operator updates use the JSONL journal.

## Workflow-owned durable state and evidence

### `project_state/`

Markdown/JSON state maintained by workflow roles. The coordinator does not
interpret arbitrary files here. Typical packaged workflows keep current
planning context, decisions, accepted work, and handoff summaries. `goal.md`
and `goal_contract.json` remain authoritative for the layer's target; agents
should not create a competing goal in project state.

`project_state/finished.md` is the outer/task-acceptance role's accepted-work
ledger. Mechanical harness completion does not append acceptance by itself.
Implementation entries should link compact eval, git, and delivery evidence.

### Evaluation directories

- `eval_checks/` contains session-scoped eval-banana YAML definitions.
- `eval_readiness/` contains task-acceptance/readiness context for later eval
  work. The worker includes the latest receipt in semantic prompt context; it
  does not force scheduler eligibility.
- `eval_receipts/` contains compact, identity-bound verdict receipts and
  canonical report copies retained independently from detailed traces.

Raw eval-banana output belongs under the attempt trace's `eval/` directory.
The attempt assignment exposes that exact absolute path as `raw_eval_output`.
Packaged eval workflows use hermetic `--no-project-config`, an explicit judge,
and pass threshold 1.0.

An eval receipt's own schema is v1 inside the v2 session protocol. It records:

- root/session/goal and optional git subject identity;
- producer workflow, iteration, attempt, and harness run;
- a unique inventory of harness-judge check IDs and definition SHA-256 values;
- effective judge provider/model/reasoning effort;
- per-check results and the all-pass verdict;
- a canonical report reference/hash and the producing attempt's exact
  `eval/report.json` reference,
  with `raw_report_sha256s` keyed exactly by `raw_report_refs`;
- creation time.

`CoordinatorService._validate_eval_receipt_artifacts()` resolves and verifies
the definition and report hashes. Every receipt, passing or failing, must have
exactly one `raw_report_refs` entry: the producing attempt's canonical
`eval/report.json`; the trace and harness identities must match the producer.
Every regular `*.yaml`/`*.yml` check found recursively beneath `eval_checks/`
must be valid, uniquely identified, included, and bound to the exact bytes
eval-banana ran; symlinks and non-files are rejected. JSON/schema failures
preserve up to eight field-qualified diagnostics in failed history and
terminal-control rejection evidence. For a passing receipt it additionally
requires the report's absolute `project_root` to match the target repository,
its absolute `output_dir` to match the producing attempt's canonical `eval/`
directory, `run_passed: true`, `pass_threshold: 1.0`, the exact receipt check
inventory with every status `passed` and exit code zero, and each check's
effective agent/model/reasoning effort to match the receipt judge. It also
recaptures live Git state and requires it to match the evaluated HEAD and
dirty-tree digest.
These are transport, provenance, and all-pass mechanics. The coordinator does
not reinterpret the LLM judge's semantic reasons (D3/D4/D8).

An eval-emitting iteration also writes `goal_check.json` beside its result. V2
contains `goal_met`, the exact receipt verdict reason, and an
`eval_receipt_ref`. It is an iteration-local projection, not a stop switch.

### Parent/child handoff directories

- `child_requests/pending/` is the v2 inbox. A request supplies a unique
  `request_id`, origin attempt/work-item context, child-scoped assignment, and
  optional hashed input references.
- `child_requests/accepted/` preserves the exact request body selected for
  dispatch. Its hash is frozen into the child contract and ledger.
- `child_requests/rejected/` preserves invalid/undispatchable pending requests
  and separate receipts with reasons and original hashes.
- `children.json` is the revisioned parent index of dispatch intent and child
  lifecycle. It records request/child identity, accepted request evidence,
  status, outcome, usage, and any failed dispatch projections.
- `child_outcomes/<request_id>.json` is written by the coordinator when the
  child becomes terminal. It reports lifecycle, usage, compact handoff/eval/
  git/delivery references when present, latest trace reference, and
  completeness flags. It is factual evidence, not parent acceptance.
- `parent_acceptance/` is separately owned by the accountable parent workflow.
  A child's `goal_met` result never automatically closes its parent.

The reader observes both v2 `pending/*.json` and the legacy flat
`child_requests/*.json` inbox. Request identity prevents v2 redispatch;
recorded dispatch state also closes the crash window between creating a child
and removing the pending file.

### `git_receipts/` and `delivery_receipts/`

The worker writes compact git boundary receipts before and after each v2
attempt. Verbose status/diff material goes into the trace, while hashes and
summary facts remain under `git_receipts/`. Workflow roles may write delivery
receipts for branches, PRs, merges, or other declared deliverables. These
compact records do not depend on detailed trace retention.

Schema-v2 receipts use `loopy-git-status-diff-v1-sha256`. The digest binds
byte-stable, non-runtime porcelain status records plus Git's staged and
unstaged binary diffs. Engine runtime session/trace/outbox/state paths are
excluded, but versioned `.loopy_loop/workflow_sets/` definitions are product
input and remain part of the subject. Untracked and nested repositories remain
visible through porcelain paths; the compact digest is not a second archive of
their complete contents, which belong in the verbose trace when observable.

## Iteration directory

Each loopy task has a recovery-critical directory:

```text
.loopy_loop/sessions/<nested session path>/iterations/<NNNN>_<workflow_id>/
├── workflow_snapshot/
│   └── <attempt_id>/
│       ├── assignment.json
│       ├── config.yaml
│       ├── prompt.txt
│       ├── workflow_contract.yaml
│       ├── root_config_snapshot.json
│       └── manifest.json
├── prompt.txt
├── result.json
├── result_text.txt
├── harness_run_id.txt
├── pending_finished_request.json
├── trace_ref.json
├── goal_check.json                 # only when the workflow emits it
└── salvage.json                    # only after applicable crash recovery
```

### `workflow_snapshot/<attempt_id>/`

Created by `assignments.materialize_workflow_snapshot()` before dispatch. It
freezes the exact selected workflow config, prompt, workflow-set contract, root
execution snapshot, identity, repository identity, and hashes for one attempt.
The worker verifies all members before executing. This prevents a mid-run
workflow edit from silently changing an already-dispatched task.

### `workflow_snapshot/<attempt_id>/assignment.json`

Created by the coordinator before dispatch, hashed into `CurrentTask`, and
verified independently by the v2 worker before the model call. It contains:

- root/current/parent session, depth, request/work-item, workflow, iteration,
  and attempt identity;
- actor kind `harness_coordinator`, workflow role, layer kind, and role
  responsibility;
- the scoped objective, expected outputs/evidence, and frozen child inputs;
- absolute paths to this checkout's relevant state, evidence, iteration, and
  trace locations;
- plain-language ownership: integrate within this session, use typed receipts
  across parent boundaries, and treat engine state as read-only;
- repository/config/workflow/goal/git provenance hashes.

Absolute paths are execution-time capabilities for agents in this checkout.
Durable records use logical references so the session tree can be inspected
after relocation. The coordinator does not enforce assignment ownership as a
filesystem ACL; violations are detected through eval/evidence (D8).

### `prompt.txt`

The exact rendered task passed to `TeamHarness.run(task=...)`, persisted before
the first provider call by `harness_runner.write_iteration_inputs()`. It
includes assignment/role context, absolute paths, pending user inputs, latest
eval-readiness context, and the frozen workflow prompt.

### `result.json`, `result_text.txt`, and `harness_run_id.txt`

`result.json` is the worker-normalized `IterationResult`: mechanical success,
text/error/failure detail, optional usage and duration, harness run ID, explicit
harness output/run-record paths, trace manifest path, and attempt ID.
`result_text.txt` and `harness_run_id.txt` are convenient projections.

`success: true` means the harness call returned normally. It is not semantic
acceptance and is not inferred from a spawned agent's exit status (D3).

### `pending_finished_request.json`

The exact HTTP completion body, written atomically after result artifacts and
before `/finished`. It is removed only after acknowledgement. If the worker
exits in that window, the next `/register` validates it against the active
attempt and records completion exactly once. With no pending body, a matching
`result.json` can be used to reconstruct the handoff.

### `trace_ref.json`

A compact link from the durable iteration to `trace:<manifest_id>:/...` for the
attempt. v2 session history records the same relocatable logical reference;
only legacy history retains the absolute manifest path supplied by an older
worker. Accepted v2 history also records the frozen assignment hash and hashes
of the exact `/finished` request and returned response.
Crash-abandonment history records the assignment hash without a finished
exchange.

### `salvage.json`

Written only when crash recovery processes at least one tracked harness run.
It records policy and per-process drain/reap/skip reports. It does not fabricate
a result. If any process remains potentially live, the coordinator refuses
replacement work until the ambiguity is resolved.

## Caller-owned attempt traces

For a v2 task, the coordinator creates the active trace during dispatch and
the worker reopens it:

```text
.loopy_loop/traces/<root_session_id>/sessions/<session_id>/attempts/<attempt_id>/
├── trace_manifest.json
├── protocol/
│   ├── task_response.json
│   ├── assignment.json
│   ├── rendered_prompt.txt
│   ├── iteration_result.json
│   ├── finished_request.json
│   └── finished_response.json          # only when observed
├── harness/
│   └── <team_harness_run_id>/
│       ├── run.json
│       ├── coordinator_input.json
│       ├── worker_sessions.json
│       ├── agents/<agent_id>/agent_assignment.json
│       └── workers/<label>/
│           ├── stdout.jsonl
│           └── stderr.log
├── agents/
├── eval/
├── git/
│   ├── before-receipt.json
│   ├── after-receipt.json
│   └── ...verbose status/diff evidence...
└── service/
    ├── finished_exchange.json          # request + response status
    └── recovery.json                   # abandoned attempts only
```

The coordinator creates `trace_manifest.json` and compact iteration
`trace_ref.json` before returning the task. The worker identity-checks that
same active manifest and writes the exact `TaskResponse` to
`protocol/task_response.json`; it does not create a competing trace.

Loopy passes the absolute `<attempt>/harness/` directory to team-harness as
`CallerContext.trace_root`. Team-harness owns one fresh run-ID child beneath
that caller-selected root and returns its exact run, output, and coordinator
input paths on structured success or failure. Loopy carries those paths in the
iteration result rather than guessing or copying a global run location.

`coordinator_input.json` is the generated system/user envelope persisted before
client/model preflight. `run.json` is the canonical run and direct-agent
catalog. Each direct spawn receives `agent_assignment.json` with its parent
attempt, delegated task, relevant absolute state and output paths, and the
harness coordinator's integration ownership. Its streams and process/provider
identity remain under the same run.

For a built-in direct spawn with `type=harness`, team-harness derives a nested
caller context automatically. It retains the same root/current session, depth,
workflow role, parent loopy attempt, and relevant state paths; changes the
parent assignment to that direct agent's absolute assignment; writes nested
runs beneath the agent output's `harness_runs/`; and records the current run as
`parent_harness_run_id`. This is nested orchestration inside the same loop
layer, not a child session. Loopy validates that lineage and recursively checks
the nested input, finalized run, and canonical stream files before calling the
direct-agent channel complete. Generic subprocesses do not receive or imply
this contract automatically.

The outer manifest records identity, per-channel status, usage, failure, a
hashed file inventory, and lifecycle. `sealed` means capture completed;
`incomplete` means a channel or capture step did not. An unexposed provider
channel is marked unavailable/not-produced rather than invented.

### Raw local traces

The trace tree is gitignored and must be treated as private local runtime data:
it contains raw observable JSON, text, binary artifacts, prompts, commands,
streams, and environment-derived values. Sealing inventories and hashes local
bytes; it is an integrity/completeness boundary, not a disclosure-safety claim.

This is not universal filesystem interception. A model/tool can write directly
to the working tree or active trace because D8 deliberately avoids a write
sandbox. Provider-native nested agents that do not pass through team-harness's
direct spawn tool cannot be observed and remain marked unavailable.

For a matching `/finished`, the coordinator writes a finalization-outbox intent
containing the exact request and an unavailable response before it commits the
state transition. Once the next response is determined, it updates the intent,
writes `service/finished_exchange.json` and (when observed)
`protocol/finished_response.json`, and seals the still-active trace. A compact
`trace_seals/<attempt_id>.json` receipt in session state binds the final
manifest and inventory hashes. Root/session/workflow/iteration/attempt identity
and the canonical trace come from the immutable, hash-bound frozen assignment.
An assignment mismatch or invalid session topology leaves the outbox for repair
rather than redirecting the seal; an omitted or different worker-supplied path
only marks capture incomplete.

Startup processes an outbox record only after durable history proves that exact
completion or crash abandonment committed. A completion interrupted after
state acceptance but before its response became durable records the response as
unavailable and seals incomplete. An abandoned attempt also seals incomplete.
An unanchored workflow-authored `sealed`/`incomplete` claim is reopened,
recorded as a protocol error, and coordinator-sealed incomplete. If an exact
attempt with an uncommitted finished intent instead commits crash abandonment,
successful abandonment finalization removes that conflicting intent.
The outbox record remains until sealing and any terminal child-outcome
`trace_sealed` refresh succeed. These failures do not alter D3 result semantics
or block future scheduling. If even the intent cannot be written, the
coordinator logs the trace I/O failure and still preserves the semantic state
transition; write-ahead retry is guaranteed only after durable intent
publication because trace storage is not an acceptance gate.

### Trace operations

- `loopy traces list` lists manifests.
- `loopy traces inspect <path-or-manifest-id>` reads one manifest and reports
  its currently observed integrity.

## Logical references

Durable evidence uses references instead of machine-specific paths:

```text
repo:/path
session:/path
root:/path
parent:/path
session:<session_id>:/path
trace:<manifest_id>:/path
```

`LogicalReferenceResolver` first validates unique IDs, declared root/parent/
depth, physical nesting, absence of topology symlinks, and trace ownership.
It then returns a confined absolute path. This makes a durable receipt portable
without letting an untrusted `../` or symlink escape its declared scope.

## Atomicity and crash model

Recovery-relevant transitions such as state, child-ledger updates, iteration
results, pending handoffs, and salvage records use unique same-directory
temporary files plus replace. JSONL inputs are appended, flushed, and fsynced
per record. Each such published file is all-or-nothing, but a parent/child
transition is not one multi-file transaction. Startup reconciliation handles
the documented dispatch/finalization windows using accepted requests,
manifests, state, and the child ledger. Initial scaffolding still includes
direct writes/touches, so atomic replacement is not a blanket guarantee for
every engine-created file.

Workflow-authored child, eval, and control files cannot be made atomic by the
engine. Packaged prompts require temp-file-plus-rename; the coordinator treats
torn/invalid output as a protocol failure rather than evidence. There is no
blanket fsync guarantee for all session files or for arbitrary agent writes.

## Legacy v1 compatibility

An existing state file without `schema_version` is interpreted as v1. On
resume, loopy-loop retains that session's historical interfaces:

- compact `session.json`, unrevisioned/v1 `children.json`, and no immutable
  goal/workflow contract or workflow snapshot requirement;
- flat `child_requests/*.json` with `workflow_set` and `goal`;
- terminal control v1 and goal-check v1;
- harness output under
  `sessions/<session>/harness_outputs/<iteration>/<run_id>/`;
- `updates_from_user.md` and iteration files without assignment/trace links.

The current reader observes these artifacts and the new directories together.
It does not reinterpret old provenance as v2. In particular, a packaged
workflow set declaring `session_protocol_version: 2` requires v2 child,
goal-check, and terminal-control artifacts; a v1-shaped output cannot satisfy
that fresh contract.
