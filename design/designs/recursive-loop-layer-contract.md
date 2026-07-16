# Design: Recursive Loop Layers, Dynamic Agent Delegation, and Execution Records

**Status:** Implemented in loopy-loop 0.7.0 with team-harness 0.5.0 and
eval-banana 0.3.2

**Date accepted:** 2026-07-15
**Applies to:** one-layer sessions, the planner/dispatcher session tree, future
deeper session trees, team-harness coordinators and their spawned agents,
evaluation ownership, state/evidence storage, and execution traces.

This document is the binding companion design for D10, D11, and D12 in
[`design/decisions.md`](../decisions.md). It describes the implemented v2
architecture and its explicit legacy boundary. Future cloud transport is
called out separately and is not described as current behavior.

The detailed investigation that led here remains in
[`design/analysis/loop-layer-state-and-trace-contract.md`](../analysis/loop-layer-state-and-trace-contract.md).
That analysis is evidence and working history; this document is the accepted
design.

---

## Executive summary

Keep the architecture that already works:

- files and git are durable truth;
- one loopy worker owns one assignment at a time;
- a parent session waits while one child session runs;
- team-harness coordinators may dynamically spawn several agents inside that
  one assignment; and
- agents and evaluation, not transport code, judge semantic quality.

The implementation adds a typed contract around the free-form work:

1. Every durable loop layer is the same recursive **session node**. One-loop,
   double-loop, and triple-loop are topologies made from that node, not separate
   state machines.
2. Every session and attempt has stable identity, explicit ownership, and
   portable artifact references. Every running coordinator and spawned agent
   receives worker-local **absolute paths** to the state and output locations it
   needs.
3. The harness coordinator is accountable for its workflow assignment but
   remains free to choose a dynamic team. Spawned agents are ephemeral delegates,
   not additional durable loop layers.
4. Parent/child requests, child outcomes, parent acceptance, evaluation, git,
   and delivery use structured receipts with provenance.
5. Each session evaluates only its own scoped goal. Descendant evidence informs
   ancestor evaluation but never closes the ancestor automatically.
6. Correctness state/evidence is separate from exhaustive execution traces.
   Traces are ignored by git, locally sealed, optionally exported, and may be
   pruned without breaking recovery or invalidating acceptance.

This adds structure at boundaries without constraining how an agent reasons,
plans, edits, delegates, or repairs work. The engine validates identities,
schemas, hashes, and state-machine shape; it does not enforce semantic plans,
filesystem permissions, model choices, branch policy, or work quality.

### Implemented contract and compatibility boundary

| Concern | Implemented v2 behavior | Compatibility boundary |
| --- | --- | --- |
| Session depth | one recursive, depth-first parent/child edge; dispatch and iterative unwind are tested through three active depths | existing v1 sessions resume in place and retain their weaker historical manifests |
| Agent context | one immutable coordinator assignment plus a team-harness-generated envelope for every direct spawn | legacy attempts without a frozen snapshot use the previous rendered-prompt path |
| Paths | durable logical references plus topology-validated, worker-local absolute paths in every v2 attempt/delegation | legacy session artifacts remain at their original paths and are not moved |
| Child handoff | request v2, accepted/rejected archives, engine-produced child outcome, and separate workflow-produced parent acceptance | both flat v1 and `pending/` request directories are read; packaged v2 sets reject a v1 request |
| Evaluation | subject-bound eval receipt with exact canonical/raw report hashes and mechanically matching passing eval-banana report, matching goal-check projection, and one declared successful terminal-control owner per layer | v1 terminal records remain readable only for a workflow contract that retains protocol v1 |
| Traces | caller-owned raw local attempt trace, explicit completeness, hashed sealing, exact local export outbox, and active-safe pruning | old `harness_outputs/` remain inspectable; cloud delivery and its data-safety policy are future work |

---

## Vocabulary: three kinds of nesting

The words “inner” and “outer” have historically referred to several different
relationships. The contract must name them separately.

| Concept | Lifetime | Meaning |
| --- | --- | --- |
| **Loopy coordinator service** | process/service | Owns scheduling, recovery, and engine state transitions |
| **Session layer** | durable | One scoped goal with its own state, decisions, evals, attempts, and optional child |
| **Workflow role** | one or more attempts | Planner, dispatcher, outer, inner, eval reviewer, eval runner, or another set-defined responsibility inside a session |
| **Harness coordinator agent** | one attempt | Receives a workflow assignment and dynamically orchestrates agents to perform it |
| **Spawned agent** | part of one attempt | An ephemeral researcher, implementer, reviewer, tester, or other delegate |
| **Child session** | durable | A new loop layer created through the parent/child protocol with its own scoped goal |

A spawned agent does not become a child session merely because it is called
“subagent” or because it spawns more provider-native helpers. A new durable
layer exists only when a session publishes a child request and the loopy
coordinator creates the child session.

This distinction matters operationally:

- D2 permits several team-harness agents to work concurrently inside one
  assignment; it prohibits several loopy workers from concurrently advancing
  the shared session tree.
- A harness coordinator may invent useful roles at runtime. Workflow-set roles
  describe durable responsibilities, not a fixed agent graph.
- Provider-native agents created inside a spawned Codex, Claude, or Gemini
  process are trace children when observable; they are not scheduling nodes.

---

## Architectural invariants

### One recursive session node

Every session layer has:

- immutable identity and topology;
- one scoped goal and goal hash;
- a workflow-set contract;
- engine scheduling and recovery state;
- semantic progress and meaningful decisions;
- user inputs addressed to that layer or its tree;
- its own check definitions, eval receipts, and terminal control;
- attempts and trace references;
- child requests, child outcomes, and parent acceptance records; and
- at most one active child.

The same parent/child edge is used at every depth:

~~~text
depth 0: program/release session
  suspended on FEATURE-4

depth 1: feature session
  suspended on TASK-4.2

depth 2: delivery session
  executing one harness assignment
~~~

“Triple loop” therefore means a three-node active path. It does not get a
special scheduler or a second child protocol.

### One deepest active assignment

At every committed transition:

1. exactly one session is the deepest active session;
2. only that session may hold a live `current_task`;
3. every ancestor on the active path is suspended on exactly one child;
4. no session has both a live task and a live child;
5. a terminal session has neither; and
6. parent and child agree on parent ID, child ID, request ID, and outcome state.

The useful per-session phases are:

| Phase | Required shape |
| --- | --- |
| `ready` | running, no task, no child |
| `executing` | running, one task, no child |
| `suspended` | running, no task, one child |
| `terminal` | terminal status, no task, no live child |

These are structural invariants. The coordinator validates and repairs the
specified recoverable torn transitions. Irreconcilable topology is surfaced as
an explicit configuration/protocol error rather than being treated as empty;
a workflow-established terminal blocker uses D5's precise
`unresolvable_error` control. The engine must never turn corrupt state into an
empty ledger and continue as if no work existed.

### State ownership is accountability, not an ACL

The loopy coordinator service is the sole writer of engine state such as
`state.json`, assignment ownership, and parent/child pointers.

Workflow roles are accountable for named semantic artifacts. A harness
coordinator may delegate their production to a spawned agent, but remains
responsible for integrating and checking the result. The contract records who
actually wrote or returned an artifact when observable.

No path sandbox enforces these responsibilities. A spawned implementation agent
may edit the target repo; a coordinator may explicitly delegate a session
artifact. Incorrect effects are detected through evidence and evaluation and
then repaired, consistent with D8.

---

## Two user-visible worlds, three implementation planes

Users and agents should reason about two worlds:

1. **State and evidence:** what the system believes, why it believes it, and
   what it needs to do next.
2. **Traces and logs:** how an execution unfolded.

The implementation separates the first world into two planes because recovery
facts and semantic judgments have different writers.

### Semantic state and compact evidence

Durable, inspectable artifacts written by workflow roles or produced as compact
receipts:

- goals, plans, progress, accepted work, and meaningful decisions;
- append-only user inputs and acknowledgements;
- child request, outcome, and parent-acceptance receipts;
- eval definitions, eval receipts, and terminal decisions;
- compact git and delivery receipts; and
- references and hashes that join those artifacts to traces.

### Recovery journal

Durable engine facts required to schedule and recover:

- session topology and state revision;
- active attempt and exact worker owner;
- staged child-transition records;
- compact process identity needed for safe drain/reap;
- task/result protocol receipts; and
- tree-wide stop and resource-accounting projections.

### Trace plane

Append-only execution detail:

- exact generated coordinator system/user inputs;
- visible model responses and compaction summaries;
- tool calls, arguments, results, and retries;
- every direct spawn's authored prompt and effective prompt;
- model, effort, command, cwd, provider session, and process identity;
- stdout, stderr, and CLI event streams;
- raw eval reports and verbose git status/diffs;
- captured coordinator/worker completion-protocol artifacts and attempt-local
  failure details; and
- timing, usage, failure, and completeness markers.

Recovery and semantic acceptance may refer to a trace manifest, but neither may
depend on a prunable trace-only fact. The compact fact needed for correctness
must also exist in state/evidence or the recovery journal.

---

## Physical layout

The implementation keeps the nested session tree for compatibility and adds a
separately ignored trace root. It never relocates an active legacy session:

~~~text
.loopy_loop/
├── repository.json                      # ignored stable checkout identity
├── sessions/
│   └── <root_session_id>/
│       ├── session.json
│       ├── parent.json                 # child sessions only
│       ├── state.json
│       ├── control.json
│       ├── control_rejected/
│       ├── protocol_failures/
│       ├── goal.md
│       ├── goal_contract.json
│       ├── children.json
│       ├── events.jsonl
│       ├── workflow_contract.json
│       ├── project_state/
│       │   ├── decisions/
│       │   └── finished.md             # legacy human-readable task ledger
│       ├── inputs/
│       │   ├── user_updates.jsonl
│       │   ├── accepted_request.json    # child sessions only
│       │   └── artifacts/               # frozen parent inputs
│       ├── updates_from_user.md        # legacy input inbox
│       ├── child_requests/
│       │   ├── pending/
│       │   ├── accepted/
│       │   └── rejected/
│       ├── child_outcomes/
│       ├── parent_acceptance/
│       ├── eval_checks/
│       ├── eval_readiness/
│       ├── eval_receipts/
│       ├── git_receipts/
│       ├── delivery_receipts/
│       ├── trace_seals/
│       ├── harness_outputs/            # legacy worker trace location
│       ├── iterations/
│       │   └── <iteration>_<workflow>/
│       │       ├── workflow_snapshot/
│       │       │   └── <attempt_id>/
│       │       │       ├── assignment.json
│       │       │       └── frozen workflow files
│       │       ├── prompt.txt
│       │       ├── result.json
│       │       ├── pending_finished_request.json
│       │       ├── salvage.json
│       │       ├── goal_check.json
│       │       └── trace_ref.json
│       └── children/<child_session_id>/...
├── traces/
│   └── <root_session_id>/
│       └── sessions/<session_id>/
│           └── attempts/<attempt_id>/
│               ├── trace_manifest.json
│               ├── protocol/
│               │   ├── task_response.json
│               │   ├── assignment.json
│               │   ├── rendered_prompt.txt
│               │   ├── iteration_result.json
│               │   ├── finished_request.json
│               │   └── finished_response.json  # when the response was observed
│               ├── harness/
│               │   └── <harness_run_id>/       # canonical team-harness run
│               │       ├── run.json
│               │       ├── coordinator_input.json
│               │       ├── worker_sessions.json
│               │       └── agents/<agent_id>/
│               │           ├── agent_assignment.json
│               │           └── harness_runs/<nested_run_id>/  # type=harness only
│               ├── agents/                     # reserved adapter channel
│               ├── eval/
│               ├── git/
│               └── service/
│                   ├── finished_exchange.json  # request + observed response status
│                   └── recovery.json            # abandoned attempts only
├── trace_export_outbox/
└── trace_finalization_outbox/
~~~

`.loopy_loop/traces/` is gitignored independently. The packaged templates
and `loopy init` additively ignore sessions, traces, both outboxes, repository
identity, and root state/lock/archive files without deleting user entries.
Product changes belong in the target repo's git history. Runtime traces use
explicit local export when a second copy is needed; a future cloud adapter can
consume the same outbox without changing the correctness path.

`pending_finished_request.json` and `salvage.json` are recovery-journal facts.
They stay with the attempt and are never prunable as trace detail. The legacy
`harness_outputs/` and workflow-created `eval_results/` directories remain
readable for old sessions; new raw eval output is routed to the attempt trace as
described below.

---

## Identity and path contract

### Persist portable identity; execute with absolute paths

Durable manifests use IDs and logical references such as:

- `repo:/path` — the target repository;
- `session:/path` — the receipt's own session;
- `root:/path` — the root session;
- `parent:/path` — the immediate parent;
- `session:<session_id>:/path` — any named session in the validated tree; and
- `trace:<trace_manifest_id>:/path` — an artifact in one trace manifest.

The grammar is `<scope>:/<relative-path>` for implicit scopes and
`<scope>:<id>:/<relative-path>` for named scopes. The resolver rejects `..`,
unknown IDs, and paths outside the validated repository/session/trace roots.

Host-specific absolute paths are not canonical durable identity because a
checkout can move or execute on another host.

This applies to completed-attempt history as well: v2 history stores the trace
manifest as a `trace:...` reference. The absolute `trace_manifest_path` field
is retained only so old session files remain readable. Accepted v2 completion
history also binds the frozen assignment SHA-256 and hashes of the
exact `/finished` request and scheduler response. Crash
abandonment history binds the assignment but has no finished exchange.

Before execution, the worker resolves every relevant logical reference and
writes the absolute value into the attempt assignment. The assignment path
itself is absolute and appears near the beginning of the effective coordinator
prompt. Directly spawned agents receive an absolute path to their own delegation
assignment as well.

Agents must not infer session paths from the current working directory or from
ambiguous strings such as `project_state/current_state.md`. The cwd remains the
target repo for normal development tools, while state and trace paths are
explicit.

### Immutable session manifest

`session.json` records topology and origin:

~~~json
{
  "schema_version": 2,
  "session_id": "session-feature",
  "root_session_id": "session-program",
  "parent_session_id": "session-program",
  "depth": 1,
  "workflow_set": "feature_planner",
  "layer_kind": "feature",
  "goal_hash": "sha256:...",
  "goal_contract_hash": "sha256:...",
  "origin": {
    "request_id": "req-feature-4",
    "parent_attempt_id": "attempt-program-12",
    "parent_work_item_id": "FEATURE-4"
  },
  "created_at": "..."
}
~~~

The goal text itself remains an immutable session file. A child receives a
session-local goal contract derived from its request; it does not accidentally
inherit the parent's completion criteria merely because execution settings are
shared.

Tree-wide execution configuration—provider, coordinator model, model tiers,
recovery policy—is separate from the session-local goal and acceptance
contract. Children inherit the former and receive the latter explicitly.

### Immutable goal contract

`goal_contract.json` is the machine-readable session-local objective:

~~~json
{
  "schema_version": 1,
  "session_id": "session-feature",
  "goal": "Deliver FEATURE-4",
  "goal_hash": "sha256:...",
  "completion_criteria": ["..."],
  "stop_criteria": ["..."],
  "constraints": ["..."],
  "deliverables": ["..."],
  "required_evidence": ["feature eval receipt", "delivery receipt"],
  "terminal_blocker_policy_ref": "workflow:feature_planner",
  "origin_request_id": "req-feature-4",
  "accepted_request_ref": "session:/inputs/accepted_request.json",
  "accepted_request_sha256": "sha256:...",
  "inputs": [
    {
      "ref": "session:/inputs/artifacts/input-0001-a1b2c3d4e5f6.artifact",
      "sha256": "sha256:..."
    }
  ],
  "created_at": "..."
}
~~~

`goal.md` is its human-readable rendering. The contract is created once with the
session and hashed by `session.json`. For a child it is derived from child
request v2; for a root it is derived from the operator-supplied target goal and
the root configuration's completion/stop criteria. Those broader root criteria
are not copied into a differently scoped child's goal contract.

### Workflow-set contract

Each workflow set ships a small declarative contract, copied or hashed into the
session:

~~~yaml
schema_version: 1
session_protocol_version: 2
layer_kind: delivery
roles:
  outer:
    responsibility: task acceptance and readiness for layer evaluation
  inner:
    responsibility: implementation planning and execution
  eval_reviewer:
    responsibility: layer-scoped outcome-check definitions
  eval_runner:
    responsibility: run checks and own terminal goal control
state:
  - path: project_state/current_state.md
    accountable_roles: [outer, inner, eval_runner]
  - path: project_state/decisions/
    accountable_roles: [outer]
eval:
  author_role: eval_reviewer
  runner_role: eval_runner
  goal_control_role: eval_runner
task_acceptance_role: outer
terminal_blocker_reporting_roles: [outer, inner, eval_reviewer, eval_runner]
child_interface: none
~~~

The contract explains responsibility to agents and gives reviews something to
audit. It is not a path permission list.

### Per-attempt assignment

Before dispatch, the coordinator atomically writes `assignment.json`, freezes
its full SHA-256 in the current task, and returns both the absolute path and
hash. The worker independently reconstructs the expected envelope, verifies the
frozen file, and then writes `prompt.txt` before team-harness starts. A minimum
assignment contains:

~~~json
{
  "schema_version": 1,
  "identity": {
    "root_session_id": "session-program",
    "session_id": "session-delivery",
    "parent_session_id": "session-feature",
    "depth": 2,
    "request_id": "req-task-4.2",
    "work_item_id": "TASK-4.2",
    "workflow_set": "inner_outer_eval",
    "workflow_id": "inner",
    "iteration": 4,
    "attempt_id": "attempt-abc"
  },
  "actor": {
    "kind": "harness_coordinator",
    "workflow_role": "inner",
    "layer_kind": "delivery",
    "responsibility": "implementation planning and execution"
  },
  "objective": {
    "goal_ref": "session:/goal.md",
    "goal_contract_ref": "session:/goal_contract.json",
    "goal_hash": "sha256:...",
    "assignment": "Implement TASK-4.2",
    "expected_outputs": [
      "repository changes",
      "verification evidence",
      "state update"
    ],
    "required_evidence": ["eval, git, and delivery receipts"],
    "accepted_request_ref": "session:/inputs/accepted_request.json",
    "accepted_request_sha256": "sha256:...",
    "input_artifacts": [
      {
        "ref": "session:/inputs/artifacts/input-0001-a1b2c3d4e5f6.artifact",
        "sha256": "sha256:...",
        "absolute_path": "/work/repo/.loopy_loop/sessions/.../inputs/artifacts/input-0001-a1b2c3d4e5f6.artifact"
      }
    ]
  },
  "absolute_paths": {
    "repo_root": "/work/repo",
    "root_session_root": "/work/repo/.loopy_loop/sessions/session-program",
    "root_state": "/work/repo/.loopy_loop/sessions/session-program/state.json",
    "session_root": "/work/repo/.loopy_loop/sessions/.../session-delivery",
    "parent_session_root": "/work/repo/.loopy_loop/sessions/.../session-feature",
    "goal": "/work/repo/.loopy_loop/sessions/.../goal.md",
    "goal_contract": "/work/repo/.loopy_loop/sessions/.../goal_contract.json",
    "project_state": "/work/repo/.loopy_loop/sessions/.../project_state",
    "finished_ledger": "/work/repo/.loopy_loop/sessions/.../project_state/finished.md",
    "eval_checks": "/work/repo/.loopy_loop/sessions/.../eval_checks",
    "eval_readiness": "/work/repo/.loopy_loop/sessions/.../eval_readiness",
    "eval_receipts": "/work/repo/.loopy_loop/sessions/.../eval_receipts",
    "children_index": "/work/repo/.loopy_loop/sessions/.../children.json",
    "children_root": "/work/repo/.loopy_loop/sessions/.../children",
    "child_requests": "/work/repo/.loopy_loop/sessions/.../child_requests/pending",
    "child_outcomes": "/work/repo/.loopy_loop/sessions/.../child_outcomes",
    "parent_acceptance": "/work/repo/.loopy_loop/sessions/.../parent_acceptance",
    "user_inputs": "/work/repo/.loopy_loop/sessions/.../inputs/user_updates.jsonl",
    "control": "/work/repo/.loopy_loop/sessions/.../control.json",
    "control_rejected": "/work/repo/.loopy_loop/sessions/.../control_rejected",
    "protocol_failures": "/work/repo/.loopy_loop/sessions/.../protocol_failures",
    "git_receipts": "/work/repo/.loopy_loop/sessions/.../git_receipts",
    "delivery_receipts": "/work/repo/.loopy_loop/sessions/.../delivery_receipts",
    "accepted_request": "/work/repo/.loopy_loop/sessions/.../inputs/accepted_request.json",
    "attempt_root": "/work/repo/.loopy_loop/sessions/.../iterations/0004_inner/workflow_snapshot/attempt-abc",
    "workflow_snapshot": "/work/repo/.loopy_loop/sessions/.../iterations/0004_inner/workflow_snapshot/attempt-abc",
    "trace_root": "/work/repo/.loopy_loop/traces/.../attempt-abc",
    "raw_eval_output": "/work/repo/.loopy_loop/traces/.../attempt-abc/eval"
  },
  "ownership": {
    "own_session": "write according to workflow role",
    "parent_session": "read/reference; communicate through receipts",
    "engine_state": "read only"
  },
  "provenance": {
    "repository_id": "repo-...",
    "root_config_sha256": "...",
    "workflow_config_sha256": "...",
    "workflow_prompt_sha256": "...",
    "workflow_contract_sha256": "...",
    "goal_contract_sha256": "...",
    "git_before_ref": "session:/git_receipts/git-before-attempt-abc.json"
  }
}
~~~

The exact rendered loopy prompt must exist on disk before the harness call.
Team-harness must likewise persist its generated coordinator system/user input
envelope before the first provider call. A crash before the first completed
turn then leaves a legible attempted input rather than an empty run record.

The coordinator and worker consume the same immutable workflow snapshot and
assignment for an attempt. Scheduler configuration, workflow contract, prompt
source, and rendered tree-global extension are copied into
`workflow_snapshot/<attempt_id>/` before dispatch and named by the hashes in
`assignment.json`. The worker does not silently reload a different live
`config.yaml` or `prompt.txt` after the scheduler selected the attempt. If the
assignment changes before or during execution, the worker restores the exact
engine-derived bytes so it can report a provenance-valid deterministic failure;
the failed iteration then gives the workflow an autonomous repair path. A
source mismatch is likewise a visible structural protocol failure.

The tree-global team-harness system extension contains only invariants shared by
every session and workflow. Layer purpose and workflow-role instructions live in
the attempt assignment; direct-spawn responsibilities live in each delegation
envelope. This prevents PM-only coordinator language from contaminating a child
implementation session while preserving one strong coordinator model under D9.

The worker and coordinator also exchange a stable repository/config identity.
A worker in the wrong checkout must not be able to execute a same-named local
workflow and post a plausible result to another repository's session.

---

## Dynamic harness delegation

### The coordinator chooses the team

The harness coordinator receives the complete layer/attempt contract and is
accountable for the workflow assignment. It decides dynamically:

- whether delegation is useful;
- how many agents to spawn;
- which tasks and roles they receive;
- which configured model tier and effort each task deserves;
- whether independent tasks can run concurrently;
- whether a failed or incomplete result should be retried, replaced, or routed
  around; and
- when enough evidence exists to synthesize the assignment result.

The workflow contract must not prescribe a static researcher/implementer/
reviewer graph. Example role suggestions are prompt guidance, not a required
topology. This preserves D9 and the current strength of the inner-loop
coordinator.

### Every direct spawn gets an automatic delegation envelope

The coordinator writes the task-specific content. Team-harness automatically
adds the ecosystem identity that should not depend on the coordinator
remembering boilerplate.

For each direct spawn it records an `agent_assignment.json` containing:

~~~json
{
  "schema_version": 1,
  "actor_kind": "spawned_agent",
  "agent_id": "agent_17",
  "parent_harness_run_id": "run-...",
  "parent_attempt_id": "attempt-abc",
  "root_session_id": "session-program",
  "session_id": "session-delivery",
  "session_depth": 2,
  "workflow_role": "inner",
  "delegated_role": "implementation",
  "delegated_task_id": "impl-auth-flow",
  "delegated_objective": "...",
  "assignment_path": "/abs/.../assignment.json",
  "agent_assignment_path": "/abs/.../agents/agent_17/agent_assignment.json",
  "output_dir": "/abs/.../agents/agent_17",
  "relevant_state_paths": ["/abs/.../project_state"],
  "expected_outputs": ["code changes", "test evidence", "concise handoff"],
  "state_responsibility": "implement and report; coordinator integrates",
  "authored_prompt": "...",
  "effective_prompt": "...",
  "created_at": "..."
}
~~~

`delegated_role`, `delegated_task_id`, and `expected_outputs` are dynamic audit
metadata chosen by the coordinator. They are not an allowlist. The recorded
prompt has two forms:

1. the coordinator-authored delegated prompt; and
2. the effective prompt after the automatic loopy/team-harness footer.

The trace also records the selected model/effort, command, provider session,
stdout/stderr, and final disposition.

### Access and accountability

A spawned agent normally needs:

- the delegated objective;
- the target repository;
- relevant read context from the owning session;
- an exact output location; and
- a clear statement that the harness coordinator, not the subagent, owns the
  loop-layer decision.

It may modify the target repository or a specifically delegated state artifact.
There is no engine write fence. The harness coordinator reviews and integrates
its effects and remains accountable for the workflow's durable output.

### Provider-native nested agents

A spawned Claude/Codex/Gemini process may itself create provider-native
subagents. Loopy records those actors and prompts only when the CLI event stream
exposes them. The trace manifest must say whether that channel was observable;
it must not claim completeness it does not have. First-class recursive actor
records for such agents require provider adapters, not inference from prose
output.

### Nested team-harness coordinators stay in the same loop layer

A direct spawn with team-harness's built-in `type=harness` is a special
observable case: it starts another harness coordinator, but it is still dynamic
delegation inside the same loopy assignment. The `nested_caller_context_v1`
capability means team-harness derives that coordinator's caller context rather
than relying on the parent prompt to repeat it. The derived context:

- retains the root session, current session, session depth, workflow role,
  parent loopy attempt, and relevant state paths;
- changes `parent_assignment_path` to the direct agent's absolute
  `agent_assignment.json`;
- records the current harness run as `parent_harness_run_id`; and
- places the nested canonical run under the direct agent's output directory at
  `harness_runs/<nested_run_id>/`.

Keeping the loop identity unchanged is deliberate: orchestration has nested,
but the durable state machine has not. The outer harness coordinator still
owns integration and the workflow-layer decision. Loopy checks this lineage,
the nested coordinator input, its finalized run record, and recursively
canonical direct-agent stream files before calling the direct-agent trace channel
complete. Automatic inheritance applies only to the built-in `type=harness`
spawn path; a generic child process is not inspected and guessed to be a
harness.

---

## Parent/child protocol

### Child request

A child request is a durable assignment, not merely a workflow-set name and
free-form goal:

~~~json
{
  "schema_version": 2,
  "request_id": "req-task-4.2",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "attempt-feature-9",
    "parent_work_item_id": "TASK-4.2",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Implement TASK-4.2",
    "completion_criteria": ["..."],
    "stop_criteria": ["..."],
    "constraints": ["..."],
    "deliverables": ["..."],
    "required_evidence": [
      "child eval receipt",
      "git receipt",
      "delivery receipt"
    ]
  },
  "inputs": [
    {
      "ref": "parent:/project_state/work_items.md",
      "sha256": "sha256:..."
    }
  ]
}
~~~

Publishing remains temp-file-plus-rename. An accepted request's exact body is
copied to its immutable accepted archive before the pending entry is consumed.
Invalid or undispatchable requests move to a rejected archive with a durable
receipt containing the exact reason. A request ID, not its filename, provides
idempotency and supersession.

Before dispatch, the coordinator resolves every `inputs[]` reference from the
parent's viewpoint and verifies its full SHA-256. It then copies those exact
bytes and the accepted request body under the new child's immutable `inputs/`
directory. The child goal contract refers only to these child-local copies;
the session origin separately retains the parent source references/hashes and
their mapping to the frozen copies. Every child attempt receives the
child-local logical references, hashes, and absolute worker paths and verifies
the bytes again before the harness call. Thus later parent progress edits
cannot mutate or permanently wedge an already accepted child assignment, while
the provenance chain still shows exactly where each input came from.

### Child outcome

When the child becomes terminal, the engine creates a factual outcome:

~~~json
{
  "schema_version": 1,
  "request_id": "req-task-4.2",
  "child_session_id": "session-delivery",
  "goal_hash": "sha256:...",
  "lifecycle": {
    "status": "goal_met",
    "stop_reason": "goal_met",
    "completed_at": "..."
  },
  "evidence_refs": {
    "handoff": "session:session-delivery:/project_state/handoff.json",
    "eval": "session:session-delivery:/eval_receipts/eval-7.json",
    "git": "session:session-delivery:/git_receipts/git-after.json",
    "delivery": "session:session-delivery:/delivery_receipts/delivery-7.json"
  },
  "trace_ref": "trace:manifest-attempt-abc:/",
  "usage": {},
  "completeness": {
    "eval_receipt_present": true,
    "delivery_receipt_present": true,
    "trace_sealed": true
  }
}
~~~

The engine reports identity, lifecycle, existence, and measured usage. It does
not say the work is good or that the parent accepted it.

### Parent acceptance

The parent workflow writes a separate disposition:

- parent request and work-item identity;
- `accepted`, `rework`, `reroute`, or terminal-blocker disposition;
- evidence reviewed;
- rationale and meaningful alternatives;
- accepted deliverables/commit references; and
- a superseding request when more work is needed.

Child `goal_met` is evidence for this decision, never the decision itself.

---

## Evaluation and terminal control

### Every layer evaluates its own goal

The invariant at any depth is:

> A session evaluates only its own scoped goal. Descendant evidence is an input
> to ancestor evaluation, never a substitute for it.

The delivery layer evaluates the task. A feature layer evaluates the integrated
feature using its child outcomes. The root evaluates the program or release
goal. A PM session therefore needs a parent/root evaluation stage; reviewing a
green child alone cannot close the PM goal.

The PM template's `loopy_loop_goal.txt` must contain the target project's real
goal and observable completion criteria. A sentence describing how the
dispatcher selects work is a mechanism description, not an authoritative goal.

### Separate eval responsibilities

Each workflow-set contract names:

- the check/policy author;
- the check runner;
- the task-acceptance owner; and
- the terminal `goal_met` control owner; and
- the roles instructed to report a genuinely terminal blocker.

For the stock `inner_outer_eval` set:

- `outer` accepts completed tasks and records “ready for layer evaluation”;
- `eval_reviewer` authors outcome-oriented `harness_judge` checks;
- `eval_runner` runs those checks, writes the eval receipt, and is the sole
  role that may request terminal `goal_met` for that session.

`outer` does not close the layer before eval runs. The packaged prompts enforce
that responsibility split without changing D3 or D4: evaluation remains
LLM-as-judge, and mechanical harness success remains non-semantic.

The outer readiness record is context, not scheduler eligibility. It is
rendered into subsequent workflow prompts so eval can understand why the layer
appears complete, but the scheduler remains cadence-driven and does not inspect
semantic state. The stock cadence runs reviewer/runner initially and again
after three successful implementation-role cycles; readiness neither forces nor
prevents a workflow from running.

The readiness record is an immutable receipt under
`eval_readiness/<readiness_id>.json`. It names the session/goal hash, producing
workflow and attempt, accepted task evidence, rationale, and timestamp. The
worker renders the latest valid receipt into subsequent layer prompts and gives
every relevant coordinator its absolute `eval_readiness/` path. It is semantic
context only; the engine never reads it to select the next workflow.

~~~json
{
  "schema_version": 1,
  "readiness_id": "ready-12",
  "subject": {
    "session_id": "session-delivery",
    "goal_hash": "sha256:..."
  },
  "producer": {
    "workflow_id": "outer",
    "attempt_id": "attempt-outer"
  },
  "accepted_evidence_refs": ["session:/project_state/finished.md"],
  "rationale": "...",
  "created_at": "..."
}
~~~

The existing `project_state/finished.md` remains the human-readable accepted-task
ledger and is rendered through the absolute `finished_ledger` assignment path.
It is not the canonical readiness receipt. Stock prompts consistently use this
absolute path; outer publishes structured readiness from that ledger.

The one sanctioned `unresolvable_error` path remains available under D5. It
is not owned exclusively by the `goal_met` control owner. Every workflow role
listed in `terminal_blocker_reporting_roles` is instructed to publish it when
that role establishes a genuinely terminal blocker. It requires a specific
reason and evidence of attempted autonomous routes; it does not require a
passing eval receipt. A spawned agent reports the blocker to its harness
coordinator, which publishes the layer-owned control record.

### Eval receipt

An eval receipt binds a verdict to its subject:

~~~json
{
  "schema_version": 1,
  "eval_id": "eval-7",
  "subject": {
    "root_session_id": "session-program",
    "session_id": "session-delivery",
    "goal_hash": "sha256:...",
    "git_commit": "...",
    "dirty_tree_digest": "..."
  },
  "producer": {
    "workflow_id": "eval_runner",
    "iteration": 7,
    "attempt_id": "attempt-eval",
    "harness_run_id": "run-..."
  },
  "checks": [
    {
      "check_id": "goal_outcome",
      "definition_sha256": "...",
      "kind": "harness_judge"
    }
  ],
  "judge": {
    "provider": "...",
    "model": "...",
    "reasoning_effort": "..."
  },
  "check_results": [
    {
      "check_id": "goal_outcome",
      "passed": true,
      "reason": "..."
    }
  ],
  "verdict": {
    "goal_met": true,
    "reason": "..."
  },
  "canonical_report_ref": "session:/eval_receipts/eval-7.report.md",
  "canonical_report_sha256": "sha256:...",
  "raw_report_refs": ["trace:trace-attempt-eval:/eval/report.json"],
  "raw_report_sha256s": {
    "trace:trace-attempt-eval:/eval/report.json": "sha256:..."
  },
  "created_at": "..."
}
~~~

The receipt and canonical report are durable state/evidence. The raw report is
trace detail and may be pruned later. Every eval receipt, passing or failing,
must cite exactly one raw artifact: the producing attempt's canonical
`eval/report.json`. Its full SHA-256 is the sole value in
`raw_report_sha256s`, keyed by that same sole value in `raw_report_refs`. The
engine resolves and verifies the canonical and raw bytes, trace identity, and
harness-run identity before a goal-check projection or terminal control can use
the receipt. Requiring the singleton raw report for a failing receipt preserves
why evaluation did not pass; the additional all-pass checks below apply only to
a passing verdict.

Every regular `*.yaml`/`*.yml` check discovered recursively below the session's
`eval_checks/` must be well-formed, uniquely identified, present in the receipt,
and bound to the SHA-256 of the exact YAML bytes eval-banana executed; symlinks
and non-files are rejected. Receipt JSON/schema failures retain up to eight
field-qualified validation messages in failed iteration history and
terminal-control rejection evidence, giving the owning workflow a concrete
repair target. For a passing verdict, the raw report must also
record the target repository's absolute project root and the producing
attempt's absolute canonical `eval/` output directory, `pass_threshold: 1.0`,
`run_passed: true`, and exactly the receipt's checks. Every check must have
status `passed`, judge exit code zero, and effective judge provider/model/
reasoning effort matching the receipt. Finally, the coordinator recaptures HEAD
and the versioned dirty-tree digest when applying terminal control, so
repository or index drift after the worker's git-after receipt cannot be
accepted as the evaluated subject. These are provenance and transport checks;
they do not reinterpret check reasons or replace the LLM-as-judge decision
(D4).

For new-schema sessions the eval receipt is canonical. A per-iteration
`goal_check.json` is only a small scheduler/history projection:

~~~json
{
  "schema_version": 2,
  "goal_met": true,
  "reason": "...",
  "eval_receipt_ref": "session:/eval_receipts/eval-7.json"
}
~~~

The engine verifies that this projection matches the cited receipt. A mismatch
is an invalid iteration protocol output and cannot be used by control. Legacy
v1 `goal_check.json` remains readable but makes only the older, weaker
provenance claim.

### Terminal control v2

Control v2 is a discriminated contract. `goal_met` requires same-session eval
evidence:

~~~json
{
  "schema_version": 2,
  "control_id": "control-8",
  "state": "stopped",
  "stop_reason": "goal_met",
  "reason": "...",
  "producer": {
    "session_id": "session-delivery",
    "workflow_id": "eval_runner",
    "attempt_id": "attempt-eval"
  },
  "eval_receipt_ref": "session:/eval_receipts/eval-7.json",
  "created_at": "..."
}
~~~

`unresolvable_error` instead requires a precise blocker, attempted routes, and
producer identity; it omits `eval_receipt_ref`:

~~~json
{
  "schema_version": 2,
  "control_id": "control-9",
  "state": "stopped",
  "stop_reason": "unresolvable_error",
  "reason": "Missing deployment credential after trying documented local and CI routes",
  "attempted_routes": ["local credential discovery", "existing CI identity"],
  "producer": {
    "session_id": "session-delivery",
    "workflow_id": "inner",
    "attempt_id": "attempt-inner"
  },
  "evidence_refs": ["session:/protocol_failures/credential-blocker.json"],
  "created_at": "..."
}
~~~

Both terminal forms belong to the session's **exact current task** when the
matching `/finished` transition is processed: producer session, workflow, and
attempt must match that live assignment. An earlier attempt, a sibling layer,
or a spawned agent cannot leave control behind for a later task to apply. A
spawned agent reports a conclusion to its harness coordinator; the current
workflow role publishes the layer-owned record.

Before applying `goal_met`, the engine additionally validates the current
producer against the frozen workflow contract's `goal_control_role`, then
validates the receipt's schema, identity, existence, subject goal hash, and
referenced artifact hashes. For `unresolvable_error` it validates that the
current producer role is listed in `terminal_blocker_reporting_roles`. These
checks do not reinterpret the semantic conclusion.

A malformed **new-schema** terminal request is a repairable protocol failure,
not proof that the goal failed. The engine uses a per-signal,
version-discriminated control reader for legacy v1 and new v2. An invalid v2
record is atomically moved to `control_rejected/` with its original hash,
reason, and producing-attempt reference; the session remains running. The
engine writes `protocol_failures/<failure_id>.json` naming the
workflow/attempt, rejected-control reference, repair reason, and counter value.
Every assignment receives the absolute rejected-control/protocol-failure paths,
and a bounded failure counter eventually stops repeated breakage. Packaged
workflows emit v2 only because their frozen workflow contract declares session
protocol 2. Legacy v1 sessions retain their historical behavior and are not
silently reinterpreted.

~~~json
{
  "schema_version": 1,
  "failure_id": "protocol-failure-4",
  "kind": "invalid_control",
  "producer": {
    "workflow_id": "eval_runner",
    "attempt_id": "attempt-eval"
  },
  "rejected_control_ref": "session:/control_rejected/control-8.json",
  "reasons": ["eval receipt goal hash does not match this session"],
  "consecutive_failure_count": 1,
  "created_at": "..."
}
~~~

The packaged eval workflow is executable from a clean initialized target with
`eval-banana>=0.3.2`. Its documented check schema and harness-judge configuration
are tested together. Reviewer and runner commands use `--no-project-config`;
runner prompts pass the attempt's absolute trace `eval/` directory as the raw
output directory and explicitly select/verify the judge agent, model, and
reasoning effort. They publish only the canonical report and compact receipt
into session state. Legacy session-local `eval_results/` directories remain
readable but are not the v2 raw-output location.

---

## Git, branches, PRs, and delivery

The engine records facts at attempt boundaries:

- repository identity and absolute path;
- branch or detached state;
- HEAD;
- porcelain status and dirty-tree digest;
- sanitized remote fingerprints; and
- timestamp and attempt identity.

Compact before/after facts live in `git_receipts/`. Verbose status, diffs, and
command output live in the trace.

The dirty-tree digest uses the versioned
`loopy-dirty-tree-v2-sha256` algorithm. It hashes every bytewise-sorted Git
index entry from `git ls-files --stage -z` (mode, object ID, stage, and path),
then the bytewise-sorted
`git status --porcelain=v1 -z --untracked-files=all` entries plus file type,
mode, and content digests for every changed tracked or untracked working-tree
path. Deleted paths get an explicit tombstone. This binds partial-staging and
unmerged-index states that can have the same HEAD, porcelain status, and
working-tree bytes but different staged content.

Untracked ignored files and engine runtime data under `.loopy_loop/sessions/`,
`.loopy_loop/traces/`, both trace outboxes, and root state/identity files are
excluded. Tracked files still contribute through the index even if an ignore
rule also matches them. Versioned product input such as
`.loopy_loop/workflow_sets/` is not runtime data and remains in the
index/digest. The compact receipt stores hashes, not file contents. This
prevents matching digests from hiding staged or untracked source while keeping
verbose diffs in the trace.

The workflow writes a delivery receipt containing:

- intended base and work branch;
- changed repositories and commits;
- PR URL/number;
- CI/check status with observation time;
- merge status and merge commit;
- blockers and remaining actions; and
- evidence references.

Parent and eval roles compare delivery claims with observed facts. The engine
does not reject an assignment simply because the tree is dirty, a PR is absent,
or a branch differs. Planning, research, local-only work, and repair can all
legitimately have those shapes. Delivery quality is evaluated and repaired
under D8.

---

## Complete observable input/output accounting

“All agent I/O” means every logical input delivered to an agent interface and
every output/effect the system can observe. It does not mean hidden
chain-of-thought or provider-internal transport bytes. Observable values are
retained as raw local trace data even when they resemble credentials.

### Inputs retained

- root goal and every child assignment;
- append-only user updates, target scope, delivery, and acknowledgement;
- frozen root execution configuration and workflow source hashes;
- exact coordinator `TaskResponse` that dispatched the attempt;
- exact loopy task/assignment envelope;
- exact rendered loopy coordinator prompt, persisted before execution;
- generated team-harness coordinator system/user inputs, persisted before the
  first provider call;
- coordinator-authored and effective direct-spawn prompts;
- model, effort, cwd, and runtime identity;
- input artifact references and hashes; and
- git-before receipt.

### Outputs retained

- visible coordinator responses and compaction summaries;
- tool calls, arguments, results, and retries;
- spawned-agent stdout/stderr and CLI event streams;
- provider session and process outcomes;
- normalized iteration result and the exact `/finished` request/response
  exchange captured by the coordinator before it seals the trace;
- git-after and delivery receipts;
- eval receipt and raw reports;
- child outcome and parent acceptance; and
- session-owned recovery and salvage records.

The trace manifest inventories these channels and records a completeness status
for each. Team-harness's caller-owned run record contains the direct actor and
provider-session catalog. Its direct-agent channel is complete only when every
recorded agent points to canonical stdout/stderr files in that run; a process
exit or a `run.json` alone is not enough.
A channel that a provider does not expose is marked unavailable rather than
guessed; provider-native nested agents are not inferred from a direct agent's
name or prose.

Loopy, team-harness, and eval-banana capture observable local bytes without a
generic credential detector. The trace tree is gitignored by default but can
contain prompts, commands, structured tool data, stdout/stderr, binary output,
environment-derived values, credentials, and other private material. Sealing
inventories and hashes those bytes without transforming them. Therefore
`sealed` is an integrity/completeness claim, never a claim that the trace is
safe to disclose. The current exporter makes an exact local copy. A future
cloud exporter must own and document an explicit data-safety policy before it
transmits anything off-host.

---

## Trace lifecycle and cloud export

Each v2 attempt owns one `trace_manifest.json` containing:

- schema and lifecycle: `active`, `sealed`, or `incomplete`;
- root/session/request/work-item/workflow/iteration/attempt/harness identities;
- raw artifact inventory with hash, size, and media type;
- input/output channel completeness;
- usage and failure summary; and
- local export status placeholder.

The direct actor tree, authored/effective spawn prompts, commands, provider
sessions, and worker streams live in the canonical team-harness run and worker
session artifacts inventoried by that manifest rather than being duplicated in
the loopy manifest.

The coordinator creates the active manifest and compact `trace_ref.json` while
it dispatches the task, before returning the HTTP `TaskResponse`. The worker
reopens that same identity-checked manifest; it does not create a competing
trace. It then records the exact dispatch response as
`protocol/task_response.json`, verifies and copies the frozen assignment, and
continues with rendered input and harness artifacts. At completion the worker
records `protocol/finished_request.json`; the coordinator owns
`service/finished_exchange.json`, which pairs that request with the exact
response it observed. `protocol/finished_response.json` exists only when a
response was actually determined. This division makes both sides of the HTTP
boundary attributable. Here and elsewhere, “exact” means the complete logical
structured payload written to the local trace without value redaction.

A stale or mismatched `/finished` call may receive the scheduler's current
response so its caller can converge, but it owns no transition. It never
appends history, creates or updates a finalization intent, records a finished
exchange, or seals the stale attempt.

Team-harness writes its complete canonical `run.json` under the caller-supplied
attempt `harness/` root and returns that exact absolute run path, run directory,
and coordinator-input path on success and structured failure. Loopy validates
those paths and the embedded caller identity in place; it does not import a
second copy or infer a private/global run location. Usage, recovery, inspection,
and export all use that same canonical run.

Trace capture is local-first. The worker keeps the attempt active through the
completion post. For a matching completion, the coordinator writes a
finalization-outbox intent with the exact request and an as-yet-unavailable
response **before** committing the state transition. Once the next
`TaskResponse` is determined, it updates that record and finalizes the trace:

1. finishes or marks incomplete every known channel;
2. hashes the manifest inventory;
3. atomically seal the manifest; and
4. writes a compact session-plane seal receipt binding the manifest hash,
   inventory root, identity, lifecycle, and seal time;
5. refreshes a terminal child's factual outcome so its `trace_sealed` flag
   reflects the new receipt; and
6. leaves export opt-in; `loopy traces export` creates/reuses an outbox record.

Startup processes a completion intent only after history proves that exact
attempt committed. If the process died after state acceptance but before the
HTTP response became durable, the retry records the response as unavailable
and seals the trace `incomplete`; it never invents the response. An intent whose
state transition did not commit remains inert until a legitimate completion
retry resolves it. If that exact attempt instead commits crash abandonment,
successful abandonment sealing removes the now-contradictory uncommitted
finished intent. The record is otherwise removed only after sealing and any
terminal child-outcome refresh succeed.

Crash abandonment uses the same ordering: its finalization intent is written
before the abandonment transition and is processed only when history proves
that exact attempt was recorded with `failure_kind: crash`. Abandoned traces
seal as `incomplete`. Trace failure remains outside scheduling and D3
semantics, but it cannot disappear silently at the state/trace crash boundary.
An agent-authored `sealed`/`incomplete` lifecycle without the compact
session-plane receipt is not an engine seal: the coordinator reopens it,
records the premature claim as a protocol error, writes the crash evidence, and
reseals incomplete. Root/session/workflow/iteration/attempt identity and the
canonical trace path come from the immutable, hash-bound frozen assignment.
An assignment hash/identity mismatch or invalid session topology leaves the
outbox visible for repair; a missing or different worker-supplied path marks
capture incomplete and can never redirect sealing.

Outbox persistence is deliberately outside semantic state correctness. If the
coordinator cannot write or update the intent at all, it logs that trace
failure and does not roll back an otherwise valid loop transition; D3 and D12
forbid turning trace storage availability into work acceptance. The
write-ahead recovery guarantee therefore applies once the intent has been
durably published, while the warning is the observable fallback for an outbox
I/O failure.

The implemented export adapter makes an idempotent, exact copy to an
operator-supplied local directory without filtering. It stages in a temporary sibling, atomically publishes the destination,
and verifies the destination inventory on reuse. Its durable outbox binds one
manifest to one destination and records pending/exported status, attempts, and
errors; an ID collision or destination drift is refused. It never participates
in scheduling or semantic acceptance. A future cloud
adapter may add asynchronous delivery, logical-path rewriting, compression,
encryption, and an explicit data-safety policy while consuming the same outbox;
none of those future transport features is claimed by the local adapter.

Pruning refuses active/unsealed attempts and v2 lifecycle claims without a
valid session-plane receipt. Once retention policy permits deletion, it may
remove an authentically sealed trace even when it observes later file drift,
reporting that failed observation; export remains byte-strict. Losing raw
traces must not remove the compact state/evidence or recovery facts needed to
understand why work was accepted or to resume the session.

---

## User updates, stop propagation, and resource accounting

V2 uses `inputs/user_updates.jsonl` instead of mutating the legacy
`updates_from_user.md` inbox. Every `loopy update` record has an input ID,
target session or `tree` scope, timestamp, and pending acknowledgement. The
worker appends an attempt delivery record and tells the assigned coordinator to
append an acknowledgement after acting; it never rewrites earlier lines. A
tree-scoped update reaches the deepest active assignment rather than waiting
for suspended ancestors to resume. The legacy markdown file remains untouched
and readable for old workflows.

A root stop request becomes visible to the deepest active session at its next
coordinator check-in. Normal stop does not invent a second mid-harness
interruption mechanism; forceful termination, if added, reuses D7's process
cleanup path.

Resource accounting distinguishes per-session facts from root-tree policy.
Completed child usage rolls upward exactly once, while the active leaf receives
the ancestor-aware total needed to evaluate a root-tree budget. `max_cost_usd`
means known coordinator-model spend across the root and all descendants;
per-session ledgers explain the total. `max_turns` remains a per-session guard
unless configuration introduces a separately named tree-wide turn limit.
Unknown usage remains explicitly unknown. The coordinator can stop when known
spend reaches `max_cost_usd`, but it cannot claim that unreported provider usage
kept actual spend below that threshold. This design does not change D2 or
introduce per-child worker pools.

---

## Implementation, release, and compatibility boundary

### Delivered contract

Loopy-loop 0.7.0 implements the accepted core in one coordinated change:

- fresh v2 session manifests, immutable scoped goal/workflow contracts,
  revisioned state, validated logical references, and absolute assignments;
- hash-bound workflow snapshots and repository/worker/attempt/assignment
  completion fencing;
- recursive child request v2 with accepted/rejected archives, engine-produced
  outcomes, workflow-owned parent acceptance, bounded corrupt-ledger repair,
  and iterative multi-level unwind;
- three-depth dispatch, root-stop projection, and root-tree usage/cost
  projection while retaining one loopy worker;
- eval-readiness context, eval receipts, goal-check projection v2, terminal
  control v2, declared role ownership, and repairable rejected control;
- append-only user inputs, compact git/delivery evidence paths, and workflow
  prompts that use the assignment's absolute layer paths;
- independently ignored attempt traces, caller-owned harness runs, direct-spawn
  envelopes, raw owned capture channels, sealed manifests, inspection,
  local idempotent export, and active-safe pruning; and
- stock delivery and PM templates with layer-scoped eval ownership, dynamic
  delegation, no human gate, and hermetic eval-banana commands.

The recursive state machine has no configured maximum depth. Its acceptance
tests exercise a three-node active path because that is the first topology that
proves the edge really recurses rather than being a special parent/child case.
Additional depths use the same transition code and should extend those tests if
they expose a new operational scale risk.

### Coordinated dependency releases

The v2 worker contract is intentionally fail-fast rather than best-effort:

- `team-harness>=0.5.0` supplies public `CallerContext`,
  `get_capabilities()`, caller-owned canonical run paths, coordinator input
  persisted before the first provider call, automatic direct-spawn assignment
  envelopes, and canonical stdout/stderr capture. Loopy requires the advertised
  `caller_run_record_v1`, `coordinator_input_v1`, `spawn_assignment_v1`,
  and `nested_caller_context_v1` capabilities in addition to its own
  assignment, snapshot, and trace capabilities.
- `eval-banana>=0.3.2` supplies `--no-project-config`, exact empty-directory
  `--flat-output`, per-check definition hashes, and explicit judge metadata.
  Packaged prompts explicitly select the judge agent/model/effort
  and verify the effective values in the canonical `eval/report.json` before
  publishing a receipt.
- Coordinator and worker must be upgraded together. A fresh v2 tree returns
  HTTP 426 before state mutation when worker protocol/capabilities are too old;
  it never silently drops semantic config fields.

During cross-repository development, install the companion team-harness and
eval-banana branches as editable dependencies. Released loopy-loop declares
these minimum versions directly in `pyproject.toml`.

The implementation is therefore delivered as three coordinated PRs, not as a
loopy-only compatibility shim. Merge/publish the team-harness 0.5.0 and
eval-banana 0.3.2 PRs before (or atomically with) the loopy-loop 0.7.0 PR, link
both companion PRs from the loopy PR, and resolve the release lock against
those published versions. Editable sibling checkouts are a development bridge,
not a production dependency declaration.

### Legacy compatibility

- A `session.json` or `state.json` with no `schema_version` is legacy v1. The
  topology resolver derives missing root/depth identity from the physical
  nested tree and parent pointers. Resume does not rewrite or relocate it.
- Existing v1 child requests and terminal control remain readable under a
  protocol-v1 workflow contract. They keep their historical, weaker
  provenance; a legacy `goal_met` is not retroactively called receipt-bound.
- Packaged/default workflow contracts declare `session_protocol_version: 2`
  and therefore reject v1 terminal control and child requests as repairable
  protocol output. A custom workflow set without `contract.yaml` receives a
  conservative derived protocol-v1 role contract and remains executable, but
  a fresh session still requires the current v2 worker handshake.
- The engine scans both legacy `child_requests/*.json` and canonical
  `child_requests/pending/*.json`. V2 accepted/rejected bodies are preserved;
  deterministic identity is synthesized for an unambiguous legacy request.
- Existing `harness_outputs/`, `updates_from_user.md`, session-local raw eval
  directories, and old run references remain inspectable. New attempts do not
  move them into the v2 trace tree.
- Active sessions are never migrated in place. New-schema guarantees apply to
  artifacts created under the new contract, not by reinterpretation of old
  evidence.

### Deliberately future transport work

The correctness/state and local trace contracts are implemented. A remote
cloud exporter is not. The current CLI export is a synchronous, local,
exact and unfiltered copy driven by a durable outbox record. A later cloud
consumer must add a declared data-safety policy and may add retry scheduling,
compression, encryption, and remote object identity without changing
scheduling or semantic acceptance. Likewise,
provider-native nested-agent detail remains `unavailable` unless the provider
CLI exposes a trustworthy channel.

---

## Verification and release gate

The coordinated regression suites divide responsibility at the same boundaries
as the implementation:

- Loopy-loop tests cover immutable child goal isolation, workflow snapshot
  hashes/tamper failure, accepted-request/input hash propagation, absolute
  assignments, repository/worker completion fencing, logical-reference
  topology/traversal, v1/v2 request/control
  compatibility, accepted/rejected/corrupt child ledgers, receipt subject and
  producer matching, rejected-control repair, recursive three-depth dispatch
  and unwind, root-stop projection, git digest behavior, append-only updates,
  trace sealing/export/pruning, and legacy recovery behavior.
- Existing reliability tests continue to cover dispatch crash windows,
  stale-result fencing, drain/reap outside the transition lock, session-stack
  recovery, event/status traversal, and usage/budget accounting. Recursive
  tests exercise the new multi-level transition rather than duplicating every
  single-edge fixture at every depth.
- Team-harness 0.5 tests its public capability API, caller-owned run paths,
  coordinator input durability before a model call, direct-spawn authored and
  effective prompts, assignment envelopes, provider-session/process capture,
  canonical stdout/stderr capture, and process-group recovery compatibility.
- Eval-banana 0.3.2 tests hermetic config loading and harness-agent validation;
  loopy template tests load the documented check definitions through the real
  eval-banana schema and assert explicit runner flags, judge verification,
  eval ownership, cadence, and runtime ignore rules.

The release gate is the full suite in all three repositories plus formatting,
lint, and type checks. Any future cloud adapter or newly exposed
provider-native channel must add its own idempotency, failure, data-boundary,
and completeness tests before the manifest may claim that channel.

---

## External review and final adjudication

Claude Code and Antigravity independently reviewed this design and D10–D12
against the current source and installed dependencies:

- [Claude Code review](../analysis/claude-code-recursive-loop-binding-review.md)
- [Antigravity review](../analysis/antigravity-recursive-loop-binding-review.md)

Their first passes agreed with the architecture and found specification seams in
control-version migration, D8-safe eval cadence, terminal-blocker ownership,
request-directory migration, mixed-version negotiation, path grammar, local
secret handling, git digests, and parent-unwind locking. Those findings were
verified against the code and incorporated where valid. Both final re-reviews
reported no remaining blocking design issue.

This document is the final adjudication, not a vote between reports. In
particular, it chooses explicit fail-fast capability negotiation over silently filtering
new semantic config for old workers; mechanical eval cadence over a
readiness-driven scheduler gate; raw gitignored local capture with a future
cloud-export safety boundary; and an iterative unwind that never carries the
transition lock through process recovery.

---

## Alternatives rejected

### Fixed subagent graphs

Rejected. The harness coordinator sees the live situation and should decide
whether it needs research, implementation, review, debugging, or no delegation.
A fixed graph would waste work and weaken the inner-loop design.

### Treat every spawned agent as a loop layer

Rejected. Ephemeral delegation and durable goal ownership have different
lifecycle, recovery, and evaluation needs. Conflating them would make every
research task a scheduler concern and destroy dynamic orchestration.

### Special one-loop, double-loop, and triple-loop schedulers

Rejected. They would duplicate transition and recovery logic. One recursive
session node and one typed parent/child edge express all three.

### Persist only absolute paths

Rejected. Agents need absolute runtime paths, but durable state must survive a
moved checkout or different worker mount. Store logical identity and render
absolute capabilities per attempt.

### Enforce responsibility with filesystem ACLs or semantic scheduler vetoes

Rejected by D8. Record intended ownership and actual effects; detect and repair
violations through evidence.

### Let a child verdict close its parent

Rejected. Goals differ by layer. A child can prove its task while the integrated
feature or release remains incomplete.

### Put correctness facts only in traces

Rejected. Traces are bulky and prunable. Compact evidence required for recovery
or acceptance stays with the session.

### Stream traces directly to the cloud

Rejected. Network availability must not become a correctness dependency. Seal
locally first. The current adapter exports synchronously to a local directory;
any future remote adapter operates through the durable outbox and outside the
correctness path.

### Replace files with a global database

Rejected for now. A database does not solve role clarity, eval subject identity,
or evidence quality, and would weaken D1's inspectable file model before the
file contract is fully defined.

---

## Primary code anchors

These anchors describe the implemented contract:

- `src/loopy_loop/coordinator_app.py` — `_advance()`, child dispatch,
  completion fencing, active-stack reconstruction, and terminal unwind;
- `src/loopy_loop/models.py` — session, task, child, control, goal-check, and
  result schemas;
- `src/loopy_loop/assignments.py` — repository identity, frozen workflow
  snapshots, absolute attempt assignments, and worker-side verification;
- `src/loopy_loop/references.py` — topology validation and portable logical
  reference resolution;
- `src/loopy_loop/worker.py` — assignment rendering, harness invocation,
  compact git evidence, trace capture, result publication, and usage discovery;
- `src/loopy_loop/harness_runner.py` — team-harness integration and mechanical
  success semantics;
- `src/loopy_loop/tracing.py` — raw capture, manifests, local export outbox, and
  active-safe pruning;
- `src/loopy_loop/recovery.py` — interrupted-run discovery and D7 drain/reap;
- `src/loopy_loop/sessions.py` — physical layout and atomic artifact helpers;
- `src/loopy_loop/templates/inner_outer_eval/` — one-layer workflow/eval roles;
  and
- `src/loopy_loop/templates/pm_planner_dispatcher/` — parent planner and child
  request workflow.

The cross-project anchors are team-harness 0.5's
`src/team_harness/caller_contract.py`, `harness.py`, and
`tools/agent_tools.py`, plus eval-banana 0.3.2's `cli.py` and `config.py`.

The existing reliability design remains the source for implemented crash and
process behavior:
[`long-running-loop-reliability.md`](./long-running-loop-reliability.md).
The existing success/evaluation design remains the source for D3/D4:
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md).
D10–D12 refine how those mechanisms compose across recursive layers and dynamic
agent teams.
