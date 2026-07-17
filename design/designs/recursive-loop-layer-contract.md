# Design: Recursive Loop Layers, Dynamic Agent Delegation, and Execution Records

**Status:** The recursive/provenance/trace baseline was implemented in
loopy-loop 0.7.0–0.7.1, team-harness 0.5.0, and eval-banana 0.3.2–0.3.5.
The protocol-v3 orchestrator/state/schedule/capability amendment accepted on
2026-07-17 is implemented in loopy-loop 0.8.0 and team-harness 0.5.4. V2
sessions retain their frozen historical contract.

**Date accepted:** 2026-07-15

**Applies to:** one-layer sessions, planner/dispatcher session trees, future
deeper trees, team-harness coordinators and their spawned agents, evaluation,
state/evidence, and execution traces.

This is the binding companion design for D10–D12 in
[`design/decisions.md`](../decisions.md). It explains the architecture and why
its boundaries exist. Exact HTTP bodies live in
[`docs/http-contract.md`](../../docs/http-contract.md); exact paths and artifact
purposes live in [`docs/session-layout.md`](../../docs/session-layout.md).
The accepted v3 completion, handoff, planning-granularity, and cross-harness
amendment is specified in
[`orchestrator-owned-completion-and-cross-harness-review.md`](./orchestrator-owned-completion-and-cross-harness-review.md).
Where this document describes v2's mandatory same-attempt eval gate for
compatibility, that newer design governs fresh protocol-v3 sessions.

## Summary

The design keeps the existing principles: files and git are durable truth, one
loopy worker advances one assignment at a time, team-harness coordinators may
delegate dynamically, and one named durable orchestrator judges semantic
completion from the available evidence.

The recursive contract is organized around six boundaries. Protocol v2 shipped
the recursive identity, portable-path, delegation, provenance, and trace
baseline; the accepted v3 amendment strengthens the semantic-state and context
parts called out below.

1. A durable loop layer is one recursive **session node**. One-loop,
   planner/dispatcher double-loop, and future triple-loop systems are different
   depths of the same state machine, not separate schedulers.
2. Each session has a scoped goal, canonical plan/state/decision/handoff spine,
   optional eval evidence, and optional child. A child result informs its
   parent but never completes the parent's broader goal automatically.
3. A team-harness coordinator owns one workflow assignment and may dynamically
   spawn researchers, implementers, reviewers, or nested harness coordinators.
   Those delegates remain inside its session layer.
4. Durable records use portable logical references. Each running coordinator
   and direct spawn also receives explicit worker-local absolute paths, so no
   agent must infer which layer or directory it owns.
5. Every coordinator sees the complete scheduled-workflow roster, conditional
   next-workflow forecast, and enabled harness/model capability roster. These
   are context for judgment, not eligibility or team-shape gates.
6. Compact correctness/recovery evidence stays with the session. Detailed
   attempt I/O stays in a separately gitignored raw trace tree with explicit
   completeness and crash-safe sealing.

This is structure at system boundaries, not programmable micromanagement. The
engine validates identity, hashes, schemas, provenance, and state-machine
shape. It does not prescribe a fixed agent graph, model choice, semantic plan,
evaluation requirement, branch policy, filesystem ACL, or deterministic
quality gate.

## Three kinds of nesting

“Inner,” “outer,” “child,” and “subagent” historically described different
relationships. The contract distinguishes them:

| Concept | Lifetime | Responsibility |
| --- | --- | --- |
| Loopy coordinator service | process | Scheduling, recovery, and engine state transitions |
| Session layer | durable | One scoped goal, semantic plan/state/handoff, decisions, attempts, optional eval evidence, and optional child |
| Workflow role | one or more attempts | A set-defined responsibility such as planner, outer, inner, eval reviewer, or eval runner |
| Harness coordinator | one attempt | Owns the workflow assignment and dynamically orchestrates agents |
| Spawned agent | part of one attempt | Performs a delegated research, implementation, review, or test task |
| Child session | durable | Owns a new scoped goal created through the parent/child protocol |

A spawned agent becomes neither a child session nor another durable state
owner merely because it is called a subagent. A new loop layer exists only
when the active session publishes a child request and loopy creates a child
session.

The stock `inner_outer_eval` layer has the same semantics as a root or a child:
its `outer` owns that session's plan, handoff, and completion decision. Parent
input is optional origin context, not a hidden requirement.

This distinction preserves both D2 and dynamic orchestration: only one deepest
loopy assignment advances at a time, while team-harness may run several agents
inside that assignment when their work is independent.

## Recursive session state machine

### One node at every depth

Every session owns:

- immutable identity, topology, scoped goal, and workflow contract;
- scheduler/recovery state and one revisioned state ledger;
- `project_state/plan.md`, `tasks/`, `current_state.md`, `decisions/`,
  `finished.md`, optional `eval_state.md`, and a rolling `handoff.json`;
- semantic progress, accepted-work evidence, and append-only user inputs;
- workflow attempts and compact result/evidence receipts;
- optional session-scoped eval definitions/receipts and orchestrator-owned
  terminal control; and
- child requests, factual child outcomes, and parent acceptance records.

The same parent/child edge composes to any depth:

```text
depth 0: program or release session, suspended on FEATURE-4
  depth 1: feature session, suspended on TASK-4.2
    depth 2: delivery session, executing one harness assignment
```

The tested three-node path proves that dispatch and unwind recurse; it is not a
special “triple-loop” implementation. No configured maximum depth exists.

### One deepest active assignment

At each committed transition:

1. exactly one session is the deepest active session;
2. only that session may have a live `current_task`;
3. each ancestor on the active path is suspended on one child;
4. no session has both a live task and a live child;
5. a terminal session has neither; and
6. both sides of an edge agree on parent, child, request, and outcome identity.

The useful phases are:

| Phase | Required shape |
| --- | --- |
| `ready` | running, no task, no child |
| `executing` | running, one task, no child |
| `suspended` | running, no task, one child |
| `terminal` | terminal status, no task, no live child |

The coordinator repairs specified torn transitions and iteratively unwinds
terminal descendants. It does not treat irreconcilable topology as an empty
ledger and continue. A genuinely terminal workflow blocker uses D5's precise
`unresolvable_error` signal; ordinary protocol breakage remains repairable.

### Ownership means accountability, not an ACL

The loopy service is the sole writer of engine facts such as `state.json`, task
ownership, and parent/child pointers. Workflow roles are accountable for named
semantic artifacts. A harness coordinator may delegate their production but
must inspect and integrate the result.

There is no path-level write fence. An implementation delegate may edit the
target repository, and a coordinator may deliberately delegate a state
artifact. The assignment records intended ownership and observable effects;
tests, reviews, optional evals, and direct inspection surface problems for the
orchestrator to repair or disposition, as required by D8.

## State/evidence and traces

Users reason about two worlds:

- **State and evidence** answer what the system believes, why, and what happens
  next. This includes goals, layer plans, accepted work, decisions, semantic
  handoffs, schedule/capability context, optional eval observations,
  git/delivery receipts, task ownership, and recovery records.
- **Traces and logs** answer how one attempt unfolded. This includes generated
  prompts, visible turns, tool and spawn I/O, commands, process/provider
  identity, stdout/stderr, raw eval output, verbose git evidence, timing, and
  usage.

The implementation keeps semantic artifacts and engine recovery facts in the
session tree while storing attempt detail under `.loopy_loop/traces/`. The
trace root is independently gitignored. Every fact needed to schedule, resume,
or justify acceptance has a compact session-side record; correctness never
depends on retaining detailed trace bytes.

The important layout is:

```text
.loopy_loop/
├── repository.json
├── sessions/<root>/
│   ├── session.json, state.json, goal.md, goal_contract.json
│   ├── workflow_contract.json, workflow_roster.json, events.jsonl, control.json
│   ├── control_rejected/, protocol_failures/
│   ├── harness_capability_roster.json, session_outcome.json
│   ├── project_state/
│   │   ├── plan.md, tasks/, current_state.md, decisions/, finished.md
│   │   └── eval_state.md, handoff.json
│   ├── inputs/, eval_checks/, eval_receipts/
│   ├── child_requests/, child_outcomes/, parent_acceptance/
│   ├── git_receipts/, delivery_receipts/, trace_seals/
│   ├── iterations/<iteration>_<workflow>/
│   │   ├── workflow_snapshot/<attempt>/assignment.json, scheduler_view.json
│   │   ├── prompt.txt, result.json, pending_finished_request.json
│   │   └── goal_check.json, trace_ref.json, salvage.json
│   └── children/<child_session>/...
├── traces/<root>/sessions/<session>/attempts/<attempt>/
│   ├── trace_manifest.json, protocol/, harness/, eval/, git/, service/
│   └── agents/
└── trace_finalization_outbox/
```

See the session-layout reference for the complete tree, legacy locations, and
writer/reader contract. Product changes remain ordinary target-repository git
changes; runtime session, trace, and outbox data are ignored by default.

## Identity, paths, and frozen assignments

### Portable records, absolute execution capabilities

Durable artifacts refer to one another through confined logical references:

- `repo:/path` for the target repository;
- `session:/path`, `parent:/path`, and `root:/path` for relative session scope;
- `session:<session_id>:/path` for a named session in the validated tree; and
- `trace:<manifest_id>:/path` for an artifact in one trace.

The resolver rejects traversal, unknown IDs, invalid topology, and paths beyond
the selected repository/session/trace root. These references survive a moved
checkout or a different worker mount.

Before execution, loopy resolves the relevant references into a frozen
`assignment.json`. The path map explicitly names absolute `layer_goal`,
`layer_goal_contract`, child-local `layer_inputs`, `layer_plan`, `layer_tasks`,
`layer_current_state`,
`layer_decisions`, `layer_finished_ledger`, `layer_eval_state`,
`layer_handoff`, `session_state`, `session_outcome`, `workflow_roster`,
`workflow_contract`, `scheduler_view`, `harness_capability_roster`,
`user_inputs`, `child_requests`, `children_index`, `child_outcomes`,
`parent_acceptance`, `git_receipts`, `delivery_receipts`, `session_control`,
attempt, and trace locations. Parent/request paths are additional origin
context, not substitutes for the session's own state; stable optional keys
include `parent_goal`, `parent_goal_contract`, `parent_handoff`, and
`accepted_child_request`. Topology-inapplicable paths are explicitly null.

The assignment's own absolute path appears near the start of the effective
prompt. Agents do not derive paths from cwd or ambiguous names such as
`project_state/current_state.md`; cwd remains the target repository for normal
development tools.

### Immutable session and goal identity

`session.json` binds the session, root, parent, depth, workflow set, layer kind,
goal hashes, creation time, and—when it is a child—the request/attempt/work-item
origin. `goal_contract.json` defines that session's goal, completion and stop
criteria, constraints, deliverables, and required evidence; `goal.md` is its
human-readable rendering.

A child derives its goal contract from its accepted request. It never inherits
the parent's differently scoped completion criteria merely because it shares
tree-wide execution settings such as provider, coordinator model, model tiers,
and recovery policy.

The same rule works in reverse: a root `inner_outer_eval` session has no parent
request and needs none. Its own goal contract and canonical state spine are
sufficient for outer to plan, implement, optionally evaluate, hand off, and
complete the layer.

### Frozen workflow and attempt contract

Each amended workflow set declares its layer kind, workflow-role
responsibilities, state accountability, a top-level orchestration/completion
owner, optional eval authors/runners, terminal-blocker reporters, and child
interface. This describes responsibility for prompts and audits; it is not a
filesystem permission list. Protocol-v2 contracts retain their historical eval
control owner only for frozen-session compatibility.

For a v2 or v3 session, the complete selected workflow contract is also stored in
coordinator-owned `state.json`. The adjacent `workflow_contract.json` and its
hash in `session.json` are agent-visible projections: the coordinator restores
them from state before freezing a later attempt if both were rewritten. This
keeps the protocol and role owners stable across attempts while leaving the
files inspectable. New stock contracts explicitly select v3. Existing explicit
v2 contracts and derived-v1 compatibility sets keep their documented behavior;
the loader must never silently reinterpret an old contract as v3.

The coordinator also freezes two readable context projections. The
session-wide `workflow_roster.json` lists every scheduled role, responsibility,
cadence, dependency, expected output, and authority. The attempt-local
`scheduler_view.json` summarizes recent mechanical history and states which
workflow would run next **if** the current attempt completes without control,
child dispatch, stop, failure, or recovery. That forecast is context, not a
promise or eligibility gate.

Before dispatch, the coordinator freezes the selected workflow config, prompt
body, workflow contract, and root execution config beneath that attempt's
`workflow_snapshot/`, records their hashes, writes the assignment atomically,
and freezes its SHA-256 in the task. The worker verifies the repository,
snapshot identity, hashes, reconstructed assignment, and absolute location
before calling a model. Scheduler and worker therefore cannot silently execute
different live files after an attempt was selected. Runtime semantic context,
such as the newest handoff/current-state revision and optional eval headline,
remains deliberately late-bound and is captured in the rendered attempt input.

The rendered loopy prompt and team-harness coordinator input are persisted
before their respective provider calls. A pre-first-turn crash still leaves a
legible record of the attempted input.

## Dynamic harness delegation

### The coordinator chooses the team

The harness coordinator receives the session/attempt contract, full scheduled
workflow roster, conditional scheduler view, and session-tree-frozen harness
capability roster. The capability roster enumerates all enabled harness
families and their configured `frontier`, `strong`, `standard`, and `economy`
model/effort bundles, including unavailable cells. The coordinator owns its
workflow result and decides whether to delegate, what roles and tasks exist,
which family/tier suits each task, what may run concurrently, whether to retry
or reroute, and when enough evidence exists to synthesize a result.

Workflow prompts should prefer parallel independent analysis and review by a
different enabled harness family for consequential work when useful. Eval-check
creation gets the strongest form of that guidance: cross-family criteria
analysis, one integrator, then different-family review of a stable draft. These
are judgment defaults, not a mandatory researcher/implementer/reviewer graph,
spawn count, or model gate. The point of the coordinator is to adapt its team
to the live situation.

### Every direct spawn knows its place

Team-harness automatically writes `agent_assignment.json` for every direct
spawn. It combines coordinator-authored task content with ecosystem context
that should not rely on remembered prompt boilerplate:

- root/current session, depth, workflow role, parent loopy attempt, and parent
  harness run;
- dynamic delegated role, task ID, objective, and expected outputs;
- absolute parent assignment, direct-agent assignment, output, and relevant
  state paths;
- the delegate's selected harness/model/effort plus the absolute capability
  roster path (a nested harness coordinator receives the full roster summary);
- the delegate's state responsibility; and
- both the authored and effective prompts.

The direct agent sees its assignment path and output location. It reports
results to the harness coordinator, which remains responsible for layer-owned
decisions and durable outputs.

Provider-native subagents are recorded only when their CLI exposes trustworthy
events. The trace marks unobservable channels unavailable rather than inferring
actors from prose.

### Nested harness coordinators remain delegates

A built-in team-harness `type=harness` spawn creates another harness
coordinator but not another loopy session. Team-harness derives a nested caller
context that preserves the root/current session, depth, workflow role, and
loopy attempt; points to the direct agent's absolute assignment; records the
parent harness run; and places the nested run under that direct agent's output.

The original harness coordinator still owns integration. A durable child layer
is created only through the loopy child-request protocol.

## Parent/child protocol

### Request and input freezing

The active dispatcher atomically publishes a versioned child request under
`child_requests/pending/`. It identifies the request and originating parent
attempt/milestone, child workflow set, scoped outcome, completion/stop criteria,
constraints, deliverables, relevant evidence, and hashed logical input
references. The planner normally selects a coherent phase, milestone, or
integrated feature outcome. The dispatcher preserves it rather than reducing
it to a prescribed leaf; the child outer owns work-package/task/PR
decomposition. The planner may split or combine outcomes when useful—the
engine does not enforce semantic size.

When the source planning record is mutable, the workflow first writes an
immutable per-request selection snapshot and hashes that snapshot in the
request. The packaged PM dispatcher uses
`project_state/dispatch_inputs/<request_id>.json`, publishes the request, and
only then changes the mutable work-item ledger to `waiting_for_child`. Hashing
the ledger directly would make its required status update look like input
tampering when the coordinator validates the request after the attempt.

The coordinator validates the request and hashes. A dispatchable body is
archived unchanged under `accepted/`, indexed, and copied into the child's
immutable `inputs/accepted_request.json`. Each declared input is resolved from
the parent's scope, hash-checked, and copied into the child's `inputs/artifacts/`.
The child contract and attempts use those child-local copies; origin metadata
retains the source-to-copy mapping. Later parent edits therefore cannot mutate
or wedge an already accepted child assignment.

An invalid or undispatchable request moves to `rejected/` with its body hash and
specific reason. This is a protocol disposition, not a human approval gate. A
workflow may publish a repaired request with a new identity. Request IDs make
retries idempotent and prevent one accepted identity from being reused with
different content.

### Outcome and parent acceptance

When the child becomes terminal, the engine writes a factual outcome containing
request/child/goal identity, lifecycle, measured usage, evidence references,
trace reference, and artifact-presence/completeness facts. The same
topology-neutral `session_outcome.json` shape is used for every v3 terminal
lifecycle, both root and child, including engine stops that have no terminal
`control.json`. The engine freezes terminal identity and accepted
control/handoff bytes in state, so later edits or restart-time regeneration
cannot rewrite the linked outcome basis. The outcome reports what happened; it
does not judge that the parent should accept it. Missing handoff remains
visible incompleteness rather than a hidden engine veto. Invalid handoff JSON
is diagnosed rather than crashing stack recovery. Delivery evidence is
resolved across the whole session—not only the terminal attempt—because
implementation, PR creation, merge, and completion synthesis commonly occur
in different attempts.

The parent workflow separately records `accepted`, `rework`, `reroute`, or a
terminal-blocker disposition, together with evidence reviewed, rationale,
deliverable/commit references, and any superseding request. Child `goal_met` is
evidence for this decision, never the decision itself.

## Optional evaluation evidence and orchestrator-owned terminal control

### Every layer owns its completion decision

Each durable session decides only its own goal. A delivery child can finish its
milestone without proving its parent's feature or release complete. The
workflow contract names one persistent orchestration/completion role:

- `outer` in the stock `inner_outer_eval` set; and
- `planner` in the stock `pm_planner_dispatcher` set.

Eval author/runner roles are optional evidence producers. The orchestrator may
use their observations, invoke an eval directly, wait for an imminent scheduled
eval shown in its scheduler view, rerun/supersede weak evidence, or decide from
other facts. The stock PM set does not duplicate child eval roles; a target's
goal may still instruct planner to run prepared program-level evals near the
end.

The stock scheduler retains mechanical cadence so the engine does not interpret
semantic readiness. A scheduled eval is an opportunity for independent
evidence, not a condition for another role to be eligible or for the session to
close.

### Eval provenance remains strict when evidence exists

A canonical eval receipt still binds:

- root/session/goal identity and evaluated git state;
- producing workflow, iteration, attempt, and harness run;
- every check definition and canonical definition hash;
- judge provider, model, and reasoning effort;
- individual results and final verdict; and
- canonical/raw report paths and hashes.

The engine validates these facts whenever it accepts or resolves the receipt,
and validates cited evidence against the exact subject. The producing workflow
must be in the frozen contract's declared runner roles rather than matching a
hard-coded `eval_runner` name. This proves what was observed. It does not make
the verdict terminal authority.

Raw report bytes/hashes are validated when a canonical receipt is accepted and
sealed into compact session state. Later cross-attempt citations validate that
accepted receipt plus subject, producer role, evaluated-git identity, and seal;
they do not require independently retained gitignored trace bytes. Trace loss
therefore cannot become a delayed completion gate under D12.

`goal_check.json` remains readable as a legacy or optional iteration
projection. An absent, non-passing, stale, or malformed advisory eval becomes a
field-qualified diagnostic. It does not flip a normally returned
`IterationResult.success`, increment a generic harness-failure counter, produce
`goal_check_broken`, consume terminal-control protocol-failure capacity, or
prevent another orchestrator turn.

### Identity-bound terminal control

Every terminal signal must come from the exact session, workflow, and attempt
currently being completed. An older attempt, sibling session, or spawned
delegate cannot leave a signal for later work to apply.

For protocol v3, `goal_met` comes from the frozen top-level completion role and
contains a non-empty rationale. Evidence references are optional and may be
empty; any asserted eval receipt may come from another attempt in the same
session and is provenance-validated when cited. The engine does not require a
passing verdict or reinterpret the orchestrator's semantic disposition.

The D5 last-resort `unresolvable_error` form still requires an allowed current
role, a specific blocker, autonomous routes already tried, and evidence when
available. It requires no eval or handoff. A spawned agent reports upward to
its harness coordinator; the accountable workflow role publishes any
layer-owned signal. No paused or waiting-for-human state exists.

Malformed control is atomically archived under `control_rejected/` and
described under `protocol_failures/`; the session remains repairable until its
configured protocol-failure cap is reached. Advisory eval diagnostics are not
control failures; a control record that affirmatively cites invalid evidence is
itself a false protocol claim and is rejected until repaired. V2 sessions,
including ones explicitly created by custom v2 contracts, retain their frozen
historical same-attempt eval/control semantics; fresh stock sessions explicitly
select v3 in the loopy-loop 0.8.0 implementation.

Exact v1/v2 compatibility and v3 control/receipt schemas are documented in the
HTTP contract.

## Git, branches, PRs, and delivery

Each attempt gets compact before/after git receipts with repository identity,
absolute checkout path, branch/detached state, HEAD, porcelain status digest,
remote fingerprints, timestamp, and attempt identity. The versioned digest
uses Git's null-delimited status plus staged and unstaged binary diffs; runtime
`.loopy_loop` data is excluded, while versioned workflow inputs remain visible.
Verbose status and diffs belong in the trace.

Workflow-owned delivery receipts record intended base/work branches, changed
repositories and commits, PR URL/number, observed CI/check status, merge state,
blockers, remaining actions, and evidence references. This lets a coordinator
track subagent sessions and delivery progress without making git hosting a new
state machine.

The engine records facts but does not reject an assignment merely because the
tree is dirty, no PR exists, or a branch differs. Research, repair, and local
work can legitimately have those shapes. The accountable orchestrator assesses
the evidence, using parent review and optional eval observations, and repairs
or dispositions problems under D8.

## Observable agent I/O and trace lifecycle

“All agent I/O” means all logical input delivered to an agent interface and all
output/effect visible to loopy, team-harness, or the provider CLI. It excludes
hidden chain-of-thought and provider-internal transport bytes.

Captured inputs include goals and child assignments, user updates, frozen
configuration and workflow sources, dispatch response, attempt assignment,
rendered coordinator input, direct-spawn authored/effective prompts, model and
runtime identity, hashed input artifacts, and git-before evidence.

Captured outputs include visible turns and summaries, tool calls/results,
spawned-agent streams and events, provider/process outcomes, normalized result,
completion request/observed response, git/delivery evidence, raw eval output,
child outcome, parent acceptance, recovery, and salvage records. Unobservable
channels are marked unavailable rather than guessed.

These are raw local execution records and can contain private prompts,
commands, environment-derived values, or binary output. The trace tree is
gitignored by default and should be handled as sensitive local data.

### One caller-owned attempt trace

The coordinator creates the active `trace_manifest.json` during dispatch and
writes the compact session-side `trace_ref.json`. The worker reopens and
identity-checks that same manifest; it does not create a competing trace. The
worker records dispatch, frozen assignment, rendered prompt, canonical
team-harness run, raw eval/git output, and the completion request beneath that
attempt.

Loopy passes the attempt's absolute `harness/` root to team-harness. The
harness returns its exact canonical run path even on structured failure.
Loopy validates that path, caller identity, coordinator input, direct-agent
assignments/streams, provider sessions, and nested-harness lineage in place.

The manifest inventories artifacts with hashes, sizes, media types, channel
status, identities, usage, and failure summary. Sealing is an integrity and
completeness claim over local bytes, not a semantic-success claim.

### Crash-safe finalization

For a matching `/finished`, the coordinator writes a gitignored finalization
intent containing the request before committing semantic state. Once the next
HTTP response is determined, it records the observed exchange, seals the
manifest, and writes a compact `trace_seals/` receipt anchoring manifest hash,
inventory root, identity, lifecycle, and time. A terminal child outcome is then
refreshed with that factual trace status.

Startup processes an intent only when durable history proves that exact
completion or crash abandonment committed. If state committed before the HTTP
response became durable, the response is marked unavailable and the trace
seals incomplete; it is never invented. A stale/mismatched completion owns no
transition or trace finalization. An unanchored workflow-authored terminal
manifest is reopened and coordinator-sealed incomplete.

Trace I/O failure is logged but does not roll back a valid semantic transition.
The write-ahead recovery guarantee begins once an intent is durable; trace
storage availability never becomes a work-acceptance gate.

## Inputs, stops, usage, and recovery

V2 user updates are append-only records in `inputs/user_updates.jsonl` with an
ID, target session or tree scope, timestamp, delivery, and acknowledgement.
A tree-scoped update reaches the deepest active assignment. Existing
`updates_from_user.md` remains readable for legacy sessions.

A root stop becomes visible to the deepest active layer at its next coordinator
check-in. Normal stop adds no second mid-harness interruption protocol;
forceful process termination reuses D7's drain/reap ownership.

Completed child usage rolls into the root-tree total exactly once while each
session keeps its own ledger. `max_cost_usd` applies to known coordinator-model
spend across the tree; unknown provider usage remains explicitly unknown.
`max_turns` remains per-session unless a separately named tree-wide limit is
introduced.

Interrupted-attempt recovery runs process drain/reap outside the state lock,
then revalidates under the lock. It first recovers durable local completion
receipts when possible; otherwise it records abandonment and resumes the same
recursive scheduler. No parent and active child are dispatched together.

## Compatibility and coordinated rollout

The released 0.7 baseline creates protocol/state v2 sessions. The 0.8.0
orchestrator-owned implementation introduces explicit protocol v3 for fresh
amended workflow contracts. Existing trees resume in place:

- missing state/session schema means v1, and topology may be reconstructed from
  physical nesting and parent pointers;
- v1 child requests, goal checks, terminal control, `harness_outputs/`, raw
  eval directories, and markdown updates remain readable under a protocol-v1
  workflow contract;
- v2 sessions retain their frozen eval-owned same-attempt completion contract;
- legacy flat and versioned pending child-request inboxes remain readable; and
- active legacy sessions are not relocated or retroactively assigned stronger
  provenance or different completion authority.

In the 0.8.0 implementation, packaged workflow contracts explicitly declare
protocol v3 and treat v1/v2-shaped output from a v3 attempt as a repairable
protocol failure. Custom sets retain the version they declare; sets without a
contract receive the conservative derived-v1 role contract. A contract file
that omits `session_protocol_version` remains pinned to the historical
explicit-contract default of v2. There is no implicit version upgrade.

The cross-repository contract is fail-fast:

- team-harness 0.5.0 supplies caller-owned run paths, input durability,
  direct-spawn envelopes/streams, nested caller context, and its capability
  API; team-harness 0.5.4 adds protocol-v3 capability-roster context transport;
- eval-banana 0.3.5 supplies hermetic config selection, exact flat output,
  a public canonical check-definition digest, effective judge metadata, exact
  judge inputs, and collision-safe per-check artifacts; and
- loopy-loop 0.7.1 requires the released v2 capabilities and returns HTTP 426
  before mutation when a fresh v2 tree meets an older worker. Loopy-loop 0.8.0
  adds and advertises roster/scheduler/handoff/control capabilities in the same
  fail-fast manner; advertising v2 support does not imply v3 support.

Support packages are published before the loopy-loop version that consumes
them, so the loopy dependency lock resolves against public artifacts. Editable
sibling checkouts are only the coordinated-development bridge.

## Verification boundaries

The three repositories test the boundary they own:

- loopy covers recursive three-depth dispatch/unwind, stop/usage projection,
  assignment and reference confinement, child request/input freezing,
  receipt/control provenance and repair, recovery, git evidence, trace
  finalization/integrity, standalone root/nested parity, completion with and
  without eval, advisory-eval diagnostics, canonical plan/handoff/outcome,
  workflow/scheduler views, phase-sized PM dispatch, and capability-roster
  rendering;
- team-harness covers caller-owned runs, pre-call coordinator input,
  direct-spawn assignments and canonical streams, nested caller identity,
  capability-roster propagation, requested/effective delegate settings, and
  structured process failures; and
- eval-banana covers hermetic execution, check hashes, flat raw output, and
  reported judge settings.

The coordinated release gate is formatting, lint, type checking, and the full
test suite in all three repositories.

## Independent implementation review

Claude Code and Antigravity independently reviewed the released v2 baseline and the
final bounded-shutdown follow-up. Both returned PASS for that scope. Their evidence and
adjudicated suggestions are recorded in the
[Claude Code review](../analysis/claude-code-recursive-loop-implementation-review.md)
and [Antigravity review](../analysis/antigravity-recursive-loop-implementation-review.md).
This does not establish implementation readiness for the later v3 amendment. The v3
implementation is present for 0.8.0/0.5.4 but still requires its own implementation
review and coordinated release sequence before publication.

## Alternatives rejected

**Fixed subagent graphs.** The coordinator sees the live problem and should
decide whether it needs research, implementation, review, debugging, or no
delegation. Cross-harness review is a preference informed by the enabled roster,
not a mandatory graph or quota.

**Treat every spawn as a loop layer.** Ephemeral tasks do not need durable goal,
recovery, and optional-evidence state. Only an explicit child request creates a
layer.

**Separate one-, double-, and triple-loop schedulers.** They would duplicate
transition and recovery logic. One recursive node and edge express all depths.

**Persist only absolute paths.** Agents need absolute runtime capabilities, but
durable records must survive a moved checkout. Store logical identity and
render absolute paths per attempt.

**Enforce ownership with ACLs or scheduler vetoes.** D8 requires accountable
effects, visible evidence, and repair/disposition rather than preventive fences.

**Let a child outcome close its parent.** The child and parent have different
goals; integration or release work may remain after a child completes.

**Require a passing eval before control.** Evaluation is valuable evidence but
does not own the layer plan or integrate all other evidence. The declared
orchestrator decides.

**Put correctness facts only in traces.** Detailed-trace retention is
independent, so compact recovery and acceptance evidence stays in the session.

**Replace files with a global database.** A database does not solve identity,
role clarity, or evidence quality and would weaken D1's inspectable model.

## Primary code anchors

- `src/loopy_loop/coordinator_app.py`: scheduling, child dispatch, completion
  fencing, active-stack recovery, terminal control, and iterative unwind.
- `src/loopy_loop/models.py`: session, task, child, eval, control, and result
  schemas.
- `src/loopy_loop/assignments.py`: repository identity, frozen snapshots,
  absolute assignments, and worker verification.
- `src/loopy_loop/references.py`: topology validation and logical references.
- `src/loopy_loop/worker.py`: assignment rendering, harness invocation,
  evidence/trace capture, and result publication.
- `src/loopy_loop/tracing.py`: attempt manifests, completeness, sealing,
  integrity, and local inspection.
- `src/loopy_loop/recovery.py` and `sessions.py`: process/session recovery and
  atomic artifact helpers.
- `src/loopy_loop/templates/inner_outer_eval/` and
  `pm_planner_dispatcher/`: standalone layer planning, optional eval evidence,
  milestone dispatch, handoff, and cross-harness prompt guidance.

Cross-project anchors are team-harness's `caller_contract.py`, `harness.py`,
and `tools/agent_tools.py`, plus eval-banana's `cli.py` and `config.py`.

Existing crash/process behavior remains governed by
[`long-running-loop-reliability.md`](./long-running-loop-reliability.md), and
D3/D4 evaluation semantics by
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md).
