# Session Layout

loopy-loop keeps two deliberately different runtime trees:

- `.loopy_loop/sessions/` is compact durable state. It contains the facts
  needed to schedule, resume, explain, and hand off a run.
- `.loopy_loop/traces/` is detailed attempt observability: prompts, harness
  records, spawned-agent assignments and streams, raw eval output, and verbose
  Git evidence.

Both trees are gitignored by the generated `.gitignore`. Session state is still
required while a run is active; gitignored does not mean disposable. Traces may
be retained, deleted, or exported independently because any evidence used by
the state machine is represented by compact, hash-bound session receipts.

Path construction is centralized in `src/loopy_loop/sessions.py`. The stock
`inner_outer_eval` and `pm_planner_dispatcher` workflow sets declare semantic
protocol v3. The persisted coordinator-state schema remains v2: state schema
and agent-facing session protocol are separate version axes.

## Recursive session tree

A root session is a direct child of `.loopy_loop/sessions/`. A requested child
is physically nested under its parent, and the same shape can recurse again.
There is no separate hard-coded tree format for one-, two-, or three-loop use.

```text
.loopy_loop/
├── repository.json
├── sessions/
│   └── <root_session_id>/
│       ├── goal.md
│       ├── goal_contract.json
│       ├── session.json
│       ├── workflow_contract.json
│       ├── workflow_roster.json
│       ├── harness_capability_roster.json       # root only; shared by the tree
│       ├── state.json
│       ├── events.jsonl
│       ├── control.json
│       ├── session_outcome.json                 # after any v3 terminal transition
│       ├── inputs/
│       │   ├── user_updates.jsonl
│       │   ├── accepted_request.json            # child only
│       │   └── artifacts/                       # frozen child inputs
│       ├── project_state/
│       │   ├── plan.md
│       │   ├── tasks/
│       │   ├── current_state.md
│       │   ├── decisions/
│       │   ├── finished.md
│       │   ├── eval_state.md
│       │   └── handoff.json
│       ├── eval_checks/
│       ├── eval_receipts/
│       │   └── accepted/                        # engine acceptance seals
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
│       ├── harness_outputs/                     # legacy-compatible projection
│       └── children/
│           └── <child_session_id>/
│               ├── parent.json
│               ├── workflow_roster.json
│               ├── ...same per-session files...
│               └── children/<grandchild_session_id>/...
├── traces/
│   └── <root_session_id>/sessions/<session_id>/attempts/<attempt_id>/
└── trace_finalization_outbox/
```

Only the deepest live session receives work. A parent with
`active_child_session_id` is suspended until that child becomes terminal. The
coordinator then records the child result, clears the pointer, and resumes the
parent. `CoordinatorService._suspended_parent_response()` and
`_resume_parent_if_active_child_completed()` implement this depth-first stack.

`.loopy_loop/repository.json` is checkout identity, not session state.
`assignments.ensure_repository_identity()` creates it so coordinator and
worker can prove that they are operating in the same checkout.

## Frozen identity, scope, and role contracts

### `goal.md` and `goal_contract.json`

`goal.md` is the exact resolved goal for this layer. A child receives its
child-scoped goal, never the parent's broader goal. `goal_contract.json` binds
that text and full hash to completion criteria, stop criteria, constraints,
deliverables, required evidence, and origin metadata.

A child's contract also binds the accepted request and every declared parent
input by logical reference and SHA-256. The exact bytes are copied into the
child's own `inputs/` tree. Later parent edits therefore cannot silently change
an already accepted child assignment.

### `session.json` and `parent.json`

`session.json` is the immutable manifest: session/root/parent identity, depth,
workflow set, layer kind, goal and contract hashes, origin provenance, and
creation time. A child also has `parent.json`. Logical-reference resolution
checks these declarations and physical nesting before allowing a cross-layer
reference.

### `workflow_contract.json`

This is the session-frozen role and authority contract. In protocol v3 it
separates orchestration from evaluation:

- `orchestration` names one role that owns the layer plan, rolling handoff, and
  successful completion decision;
- `evaluation` names optional check authors and runners; their results are
  advisory evidence; and
- `terminal_blocker_reporting_roles` names roles allowed to report the D5
  last-resort `unresolvable_error`.

The stock `inner_outer_eval` contract has four scheduled roles. `outer` owns
the plan, handoff, leaf acceptance, and `goal_met`; `inner` executes one
outer-selected leaf; `eval_reviewer` optionally authors checks; and
`eval_runner` optionally publishes observations. The same workflow set works
as either a root or a child.

The stock `pm_planner_dispatcher` contract has only `planner` and `dispatcher`.
`planner` owns the high-level milestone plan, child acceptance/rerouting,
handoff, and `goal_met`. `dispatcher` transports one planner-selected outcome
into a typed child request. PM-level scheduled eval roles are intentionally not
duplicated; the planner may obtain or run optional final evidence itself.

The complete parsed contract is also held in engine-owned `state.json`. Before
dispatch, the coordinator restores rewritten on-disk contract projections from
that trust root and freezes the exact bytes into the attempt snapshot.

### `workflow_roster.json`

Protocol v3 creates this once per session from the frozen workflow definitions
and contract. It lists every scheduled role with its responsibility, cadence
(`priority`, `run_every`, `run_on_start`, `must_follow`,
`run_after_successes`, and related fields), expected outputs, and authorities.
It is the inspectable answer to “which durable roles exist in this layer and
what are they responsible for?” It is not a catalog of agents dynamically
spawned inside one team-harness run.

### `harness_capability_roster.json`

Protocol v3 creates one roster at the root and every descendant references the
same absolute file. It freezes:

- the configured harness-coordinator provider/model;
- every enabled delegate harness family; and
- the `frontier`, `strong`, `standard`, and `economy` model/effort bundle for
  each family, with missing cells explicitly marked unavailable.

The roster contains no credentials. It is guidance and audit context, not a
model-policy gate. Prompts use tier names, while concrete model IDs remain in
configuration and this generated roster. The worker also forwards the path,
hash, and compact roster object through team-harness caller context so nested
harness coordinators see the same tree-wide catalog.

## Coordinator state and event projection

### `state.json`

`state.json` is the engine-owned scheduling source of truth. Its v2 persisted
schema contains the current task, iteration history, failure and usage ledgers,
active-child pointer, frozen execution and workflow contracts, and terminal
state. For protocol v3 it additionally keeps the root capability roster trust
root, the latest observed handoff revision/hash, and accepted eval-receipt
seals.

The convenient phase (`ready`, `executing`, `suspended`, or `terminal`) is
derived from status, current task, and child pointer. Agents receive the
absolute `state.json` path as read-only engine state and request transitions by
publishing typed workflow-owned files.

### `events.jsonl`

This is a best-effort post-commit observability stream. Consumers must tolerate
duplicates, gaps, and a truncated last line and deduplicate by `event_id`.
Scheduling and recovery use `state.json`, not this projection. Protocol-v3
observations include `eval_observation` and `handoff_observed`; neither is a
semantic acceptance gate.

## Protocol-v3 semantic state spine

`sessions._create_v3_semantic_spine()` scaffolds the same compact state in
every root and child:

| Path | Accountable owner | Purpose |
| --- | --- | --- |
| `project_state/plan.md` | layer orchestrator | Current outcomes, dependencies, revision, active selection, and replanning triggers for this layer |
| `project_state/tasks/` | orchestrator; leaf/dispatcher may contribute evidence | Stable per-task objective, status, dependencies, and accepted evidence |
| `project_state/current_state.md` | orchestrator | Short resumption view: active outcome, blockers, risks, and next decision |
| `project_state/decisions/` | orchestrator | Durable choices and rationale later attempts should not rediscover |
| `project_state/finished.md` | orchestrator | Append-only accepted-work ledger with commit, PR, test, review, and delivery references |
| `project_state/eval_state.md` | orchestrator; eval roles contribute observations | Optional evaluation intent, observations, disagreement, provenance, and possible next action |
| `project_state/handoff.json` | orchestrator | Rolling semantic summary for a parent or operator |

The coordinator scaffolds headings and a revision-zero handoff but does not
parse plan prose, choose tasks from it, or judge semantic sufficiency. `outer`
or `planner` keeps the spine coherent. `inner` executes the selected task and
reports evidence; it does not create the layer plan when selection is absent.

`handoff.json` binds the session and goal, a monotonically increasing revision,
the producing workflow/attempt, a summary, and flexible accepted/open/risk/
decision/evidence/delivery/eval lists. The engine validates its structure and
records continuity diagnostics. A missing, malformed, or non-monotonic handoff
does not override an authentic orchestrator completion decision.

Protocol v3 retires `eval_readiness/`; readiness and evaluation headlines live
in `project_state/eval_state.md`, and scheduled-role timing is visible in the
attempt's scheduler view. Frozen v2 sessions retain `eval_readiness/`.

## Append-only user input

`inputs/user_updates.jsonl` is the channel used by `loopy update`. An update
records its ID, target, routed session, timestamp, exact text, and pending
acknowledgement state. Before an attempt, `worker._semantic_prompt_context()`
records delivery and renders pending inputs. After acting, the workflow appends
an acknowledgement rather than editing earlier lines.

## Attempt snapshot and absolute assignment paths

Every modern task has a recovery-critical iteration directory:

```text
iterations/<NNNN>_<workflow_id>/
├── workflow_snapshot/<attempt_id>/
│   ├── assignment.json
│   ├── scheduler_view.json              # protocol v3
│   ├── config.yaml
│   ├── prompt.txt
│   ├── workflow_contract.yaml
│   ├── root_config_snapshot.json
│   └── manifest.json
├── prompt.txt
├── result.json
├── result_text.txt
├── harness_run_id.txt
├── pending_finished_request.json
├── trace_ref.json
├── goal_check.json                      # optional/legacy projection
└── salvage.json                         # only after applicable recovery
```

`materialize_workflow_snapshot()` freezes the scheduler-selected workflow
configuration, prompt, contract, root execution snapshot, repository identity,
and hashes. `assignment.json` then binds one actor, objective, checkout,
session, workflow, iteration, and attempt. The worker verifies the snapshot,
reconstructs the assignment independently, and checks the coordinator's frozen
assignment SHA-256 before calling the harness.

Protocol-v3 `assignment.json` has schema version 2 and carries absolute paths
under stable keys. The semantic and orchestration keys are:

```text
layer_goal                 layer_goal_contract
layer_inputs               layer_plan
layer_tasks                layer_current_state
layer_decisions            layer_finished_ledger
layer_eval_state           layer_handoff
session_state              session_outcome
workflow_contract          workflow_roster
scheduler_view             harness_capability_roster
user_inputs                child_requests
children_index             child_outcomes
parent_acceptance          git_receipts
delivery_receipts          session_control
attempt_root               trace_root
```

The full map also retains operational aliases such as `repo_root`,
`session_root`, `eval_checks`, and `eval_receipts`. Stable origin keys
`parent_goal`, `parent_goal_contract`, `parent_handoff`, and
`accepted_child_request` are absolute paths for a child and explicit nulls when
inapplicable. `layer_inputs` always means this layer's own immutable input
directory.

The assignment's `context` embeds compact copies of the workflow roster,
scheduler view, capability roster, and current layer identity; `provenance`
hashes their canonical files. The rendered prompt prints the authoritative
assignment path and every named absolute path. Agents must use those values
rather than infer `.loopy_loop/sessions/...` from cwd or directory names.

Absolute paths are execution-time coordinates for this checkout. Durable
cross-file evidence uses logical references so a completed tree remains
inspectable after relocation. Ownership guidance is not implemented as a
filesystem ACL (D8).

### `scheduler_view.json`

This attempt-frozen artifact shows recent mechanical history and a conditional
next-role forecast. The coordinator simulates the current attempt returning as
a mechanical success and runs the unchanged scheduler against that projected
history. The view records the possible next workflow, reasons, and assumptions
that no terminal control, child request, failure, update, stop, or recovery
changes state.

It is not a reservation. Its purpose is to let an orchestrator reason about an
imminent scheduled evaluator or reviewer without falsely promising what will
happen after the current attempt.

## Optional evaluation evidence

`eval_checks/` contains session-scoped check definitions. The stock
`inner_outer_eval` check authors use outcome-oriented `harness_judge` checks;
repo-owned test suites and prepared evaluations may provide additional
evidence. `eval_receipts/` contains compact identity- and hash-bound verdicts.
Raw reports remain under the producing attempt's trace `eval/` directory.

In protocol v3 evaluation is advisory:

- a missing, malformed, or non-passing observation does not turn a normally
  returned harness result into failure, consume the workflow failure budget,
  or prevent the orchestrator from receiving another turn;
- any declared `evaluation.check_runner_roles` workflow may produce a receipt;
  this is not hard-coded to a role named `eval_runner`; and
- `goal_check.json`, when present, is an optional/legacy iteration projection,
  not a session stop switch.

When an authorized runner finishes, `_accept_current_eval_receipts()` validates
the current-attempt receipt and its raw/canonical bytes, check definitions,
subject, producer, harness run, judge settings, and evaluated Git identity.
Each valid receipt gets an engine-owned sidecar in
`eval_receipts/accepted/<eval_id>.json`, and the same acceptance seal is stored
in `state.json`. The sidecar binds receipt reference/hash, subject, producer,
evaluated Git identity, and acceptance time.

Later `control.json` may cite accepted receipts from earlier attempts. Control
validation checks the compact receipt against its seal and current
session/root/goal/authorized-runner identity; it does not need to reopen raw
trace bytes. Citing a stale, foreign, modified, or unaccepted receipt is a
false provenance claim and invalidates that control record. Merely having a
bad advisory receipt does not.

## Terminal control and topology-neutral outcome

`control.json` begins as a neutral running record. In protocol v3 a terminal
record must identify the exact current session/workflow/attempt. `goal_met`
must come from the contract's `orchestration.completion_role`; it may cite
logical evidence, zero or more accepted eval receipts, and the canonical
handoff. No eval receipt, passing verdict, or same-attempt eval is required.

`unresolvable_error` is the D5 last resort. It must come from an authorized
role and list autonomous routes already tried. There is no paused or
waiting-for-human state. The exact schemas and rejection behavior are in
[`http-contract.md`](./http-contract.md#protocol-v3-terminal-control).

After any v3 terminal transition, `_ensure_session_outcome()` writes
`session_outcome.json`. This includes orchestrator control (`goal_met` or
`unresolvable_error`) and engine lifecycle stops such as `max_turns`,
`workflow_failure_cap`, or `stop_requested`. The projection has the same shape
for a root or child and binds:

- session/root/goal identity, terminal status, and the frozen transition
  revision/timestamp;
- stop reason and, for control-owned stops, the exact accepted control hash;
- handoff status (`valid`, `missing`, `invalid`, or `non_monotonic`) plus its
  reference/hash/revision when available;
- the exact control reason or factual engine stop reason as fallback when no
  valid handoff exists; and
- evidence, session-wide delivery, accepted eval, and trace-seal references.

The coordinator stores accepted terminal-control and valid-handoff bytes in
`state.json`. Outcome regeneration restores those exact bytes if mutable files
were later changed, so restart or trace-finalization refreshes cannot rewrite
the terminal basis. Trace-seal references may grow as asynchronous trace
finalization completes; terminal identity, control, and handoff do not.

A v3 parent's `child_outcomes/<request_id>.json` does not synthesize a second
story. It contains the request/child identity and a logical reference plus hash
of that child's `session_outcome.json`. The parent orchestrator separately
records `accepted`, `rework`, or `reroute` in `parent_acceptance/`; a child's
`goal_met` never completes its parent.

Invalid terminal control is moved unchanged into `control_rejected/`, with a
reasoned compact record in `protocol_failures/`. The engine restores a running
placeholder so a later attempt can repair the protocol. This enforces identity,
schema, containment, and truthful provenance, not semantic sufficiency.

## Child request and acceptance records

A recursive workflow atomically publishes a schema-v2 request under
`child_requests/pending/`. It supplies a unique request ID, exact origin
attempt, child workflow set, scoped outcome goal and criteria, and optional
hashed input references. A valid request is copied unchanged into
`accepted/`, indexed in revisioned `children.json`, and frozen into the child.
An invalid request is archived in `rejected/` with its original hash and
diagnosis. This is autonomous disposition, not a human approval gate.

The stock PM planner selects a phase- or milestone-sized outcome; the
dispatcher faithfully transports it. The child `outer` owns decomposition into
leaves. `parent_acceptance/` remains a distinct parent-owned semantic decision
after the child result arrives.

## Git, delivery, and trace receipts

The worker writes compact Git boundary receipts before and after every modern
attempt. Verbose status and diff bytes stay in traces. Workflow roles may add
delivery receipts for branches, PRs, merges, or other declared deliverables.
`project_state/finished.md` should link accepted work to these receipts.

Trace-seal receipts under `trace_seals/` bind the final attempt manifest and
inventory without copying raw traces into durable state. Trace finalization is
best effort with a durable outbox; failure to capture a trace does not roll back
an accepted semantic transition.

## Caller-owned attempt traces

```text
.loopy_loop/traces/<root_session_id>/sessions/<session_id>/attempts/<attempt_id>/
├── trace_manifest.json
├── protocol/
│   ├── task_response.json
│   ├── assignment.json
│   ├── rendered_prompt.txt
│   ├── iteration_result.json
│   ├── finished_request.json
│   └── finished_response.json
├── harness/<team_harness_run_id>/
│   ├── run.json
│   ├── coordinator_input.json
│   ├── worker_sessions.json
│   ├── agents/<agent_id>/agent_assignment.json
│   └── workers/<label>/{stdout.jsonl,stderr.log}
├── eval/
├── git/
└── service/{finished_exchange.json,recovery.json}
```

Loopy supplies the absolute attempt `harness/` root to team-harness. Direct
spawns receive their own assignment/output paths plus relevant layer-state
paths. A built-in `type=harness` spawn receives nested caller context,
including the root/current layer identity and root capability roster, and
writes beneath that direct agent's `harness_runs/`. It is a dynamic delegate
inside the current attempt, not a durable child session.

Raw traces may contain prompts, commands, provider output, environment-derived
values, and arbitrary files. A seal proves the observed local bytes and their
inventory; it is not a disclosure-safety claim or universal filesystem
interception.

## Logical references and atomicity

Durable references use these forms:

```text
repo:/path
session:/path
root:/path
parent:/path
session:<session_id>:/path
trace:<manifest_id>:/path
```

`LogicalReferenceResolver` validates declared topology, physical nesting,
symlinks, and scope before returning a confined absolute path.

Engine-owned recovery-critical JSON uses same-directory temporary files plus
atomic replace. JSONL inputs are append/flushed/fsynced per record. A recursive
transition still spans several files, so startup reconciliation closes known
dispatch and finalization windows. Workflow-authored control, child, eval, and
semantic files should also use temp-file-plus-rename; invalid or torn output is
diagnosed rather than trusted.

## Frozen v1/v2 sessions

The implementation still reads historical sessions under their frozen
contract. It does not reinterpret them as v3:

- v1 may lack immutable goal/workflow contracts, assignment snapshots, and
  revisioned child ledgers and may use flat child requests, control v1,
  `updates_from_user.md`, and `harness_outputs/`;
- v2 uses identity-bound control and requires its declared goal-control role,
  same-attempt passing eval receipt, and matching `goal_check.json` for
  successful completion; and
- v2 retains `eval_readiness/` and the historical singular
  `eval_receipt_ref`.

Fresh stock templates explicitly declare protocol v3. An explicit custom
contract that omits `session_protocol_version` is still interpreted as v2;
sets with no contract derive conservative v1 behavior. Compatibility is tied
to the frozen session contract, not negotiated down by a worker.
