# Design: Recursive Loop Layers, Dynamic Agent Delegation, and Execution Records

**Status:** Implemented and released in loopy-loop 0.7.0, team-harness 0.5.0,
and eval-banana 0.3.2. The canonical eval-definition digest interoperability
and complete per-check trace fix ships in loopy-loop 0.7.1 with eval-banana
0.3.5.

**Date accepted:** 2026-07-15

**Applies to:** one-layer sessions, planner/dispatcher session trees, future
deeper trees, team-harness coordinators and their spawned agents, evaluation,
state/evidence, and execution traces.

This is the binding companion design for D10–D12 in
[`design/decisions.md`](../decisions.md). It explains the architecture and why
its boundaries exist. Exact HTTP bodies live in
[`docs/http-contract.md`](../../docs/http-contract.md); exact paths and artifact
purposes live in [`docs/session-layout.md`](../../docs/session-layout.md).

## Summary

The design keeps the existing principles: files and git are durable truth, one
loopy worker advances one assignment at a time, team-harness coordinators may
delegate dynamically, and agents plus evaluation judge semantic quality.

The implemented contract adds five boundaries:

1. A durable loop layer is one recursive **session node**. One-loop,
   planner/dispatcher double-loop, and future triple-loop systems are different
   depths of the same state machine, not separate schedulers.
2. Each session has a scoped goal, state, decisions, evals, and optional child.
   A child result informs its parent but never completes the parent's broader
   goal automatically.
3. A team-harness coordinator owns one workflow assignment and may dynamically
   spawn researchers, implementers, reviewers, or nested harness coordinators.
   Those delegates remain inside its session layer.
4. Durable records use portable logical references. Each running coordinator
   and direct spawn also receives explicit worker-local absolute paths, so no
   agent must infer which layer or directory it owns.
5. Compact correctness/recovery evidence stays with the session. Detailed
   attempt I/O stays in a separately gitignored raw trace tree with explicit
   completeness and crash-safe sealing.

This is structure at system boundaries, not programmable micromanagement. The
engine validates identity, hashes, schemas, provenance, and state-machine
shape. It does not prescribe a fixed agent graph, model choice, semantic plan,
branch policy, filesystem ACL, or deterministic quality gate.

## Three kinds of nesting

“Inner,” “outer,” “child,” and “subagent” historically described different
relationships. The contract distinguishes them:

| Concept | Lifetime | Responsibility |
| --- | --- | --- |
| Loopy coordinator service | process | Scheduling, recovery, and engine state transitions |
| Session layer | durable | One scoped goal, semantic state, decisions, evals, attempts, and optional child |
| Workflow role | one or more attempts | A set-defined responsibility such as planner, outer, inner, eval reviewer, or eval runner |
| Harness coordinator | one attempt | Owns the workflow assignment and dynamically orchestrates agents |
| Spawned agent | part of one attempt | Performs a delegated research, implementation, review, or test task |
| Child session | durable | Owns a new scoped goal created through the parent/child protocol |

A spawned agent becomes neither a child session nor another durable state
owner merely because it is called a subagent. A new loop layer exists only
when the active session publishes a child request and loopy creates a child
session.

This distinction preserves both D2 and dynamic orchestration: only one deepest
loopy assignment advances at a time, while team-harness may run several agents
inside that assignment when their work is independent.

## Recursive session state machine

### One node at every depth

Every session owns:

- immutable identity, topology, scoped goal, and workflow contract;
- scheduler/recovery state and one revisioned state ledger;
- semantic progress, meaningful decisions, and append-only user inputs;
- workflow attempts and compact result/evidence receipts;
- session-scoped eval definitions, eval receipts, and terminal control; and
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
evaluation detects bad results and the loop repairs them, as required by D8.

## State/evidence and traces

Users reason about two worlds:

- **State and evidence** answer what the system believes, why, and what happens
  next. This includes goals, progress, decisions, child handoffs, eval results,
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
│   ├── workflow_contract.json, events.jsonl, control.json
│   ├── project_state/, inputs/, eval_checks/, eval_receipts/
│   ├── child_requests/, child_outcomes/, parent_acceptance/
│   ├── git_receipts/, delivery_receipts/, trace_seals/
│   ├── iterations/<iteration>_<workflow>/
│   │   ├── workflow_snapshot/<attempt>/assignment.json
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
`assignment.json`. That assignment gives the harness coordinator explicit
absolute paths to its repository, own/parent/root sessions, scoped goal,
project state, eval, child handoff, control, git/delivery evidence, attempt, and
trace locations. Its own absolute assignment path appears near the start of
the effective prompt. Agents do not derive paths from cwd or ambiguous names
such as `project_state/current_state.md`; cwd remains the target repository for
normal development tools.

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

### Frozen workflow and attempt contract

Each workflow set declares its layer kind, workflow-role responsibilities,
state accountability, eval author/runner/control owners, task-acceptance owner,
terminal-blocker reporters, and child interface. This describes responsibility
for prompts and audits; it is not a filesystem permission list.

For a v2 session, the complete selected workflow contract is also stored in
coordinator-owned `state.json`. The adjacent `workflow_contract.json` and its
hash in `session.json` are agent-visible projections: the coordinator restores
them from state before freezing a later attempt if both were rewritten. This
keeps the protocol and role owners stable across attempts while leaving the
files inspectable. An explicit `contract.yaml` that omits
`session_protocol_version` selects v2; only a workflow set with no contract at
all uses the documented derived-v1 compatibility path.

Before dispatch, the coordinator freezes the selected workflow config, prompt
body, workflow contract, and root execution config beneath that attempt's
`workflow_snapshot/`, records their hashes, writes the assignment atomically,
and freezes its SHA-256 in the task. The worker verifies the repository,
snapshot identity, hashes, reconstructed assignment, and absolute location
before calling a model. Scheduler and worker therefore cannot silently execute
different live files after an attempt was selected. Runtime semantic context,
such as the newest eval-readiness record, remains deliberately late-bound and
is captured in the rendered attempt input.

The rendered loopy prompt and team-harness coordinator input are persisted
before their respective provider calls. A pre-first-turn crash still leaves a
legible record of the attempted input.

## Dynamic harness delegation

### The coordinator chooses the team

The harness coordinator receives the session/attempt contract and owns its
workflow result. It decides whether to delegate, what roles and tasks exist,
which model tier and effort suit each task, what may run concurrently, whether
to retry or reroute, and when enough evidence exists to synthesize a result.

Workflow prompts may suggest useful roles but must not encode a mandatory
researcher/implementer/reviewer graph. The point of the coordinator is to
adapt its team to the live situation.

### Every direct spawn knows its place

Team-harness automatically writes `agent_assignment.json` for every direct
spawn. It combines coordinator-authored task content with ecosystem context
that should not rely on remembered prompt boilerplate:

- root/current session, depth, workflow role, parent loopy attempt, and parent
  harness run;
- dynamic delegated role, task ID, objective, and expected outputs;
- absolute parent assignment, direct-agent assignment, output, and relevant
  state paths;
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

The active workflow atomically publishes a v2 child request under
`child_requests/pending/`. It identifies the request and originating parent
attempt/work item, child workflow set, scoped goal, completion/stop criteria,
constraints, deliverables, required evidence, and hashed logical input
references.

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
trace reference, and artifact-presence/completeness facts. It reports what
happened; it does not judge that the parent should accept it.

The parent workflow separately records `accepted`, `rework`, `reroute`, or a
terminal-blocker disposition, together with evidence reviewed, rationale,
deliverable/commit references, and any superseding request. Child `goal_met` is
evidence for this decision, never the decision itself.

## Evaluation and terminal control

### Every layer evaluates its own goal

The delivery layer evaluates its task. A feature layer evaluates the integrated
feature using child evidence. The root evaluates the program or release. A
green leaf cannot close a broader ancestor goal.

Each workflow contract names the check author, check runner, task-acceptance
owner, terminal `goal_met` owner, and roles allowed to report a terminal
blocker. In the stock `inner_outer_eval` set:

- `outer` accepts task evidence and records readiness for layer evaluation;
- `eval_reviewer` authors outcome-oriented `harness_judge` checks; and
- `eval_runner` executes them, writes the receipt, and alone may request
  session `goal_met`.

Readiness is immutable semantic context rendered into later prompts. It is not
a scheduler condition: the stock scheduler retains mechanical cadence so the
engine does not interpret work quality. This preserves D3, D4, and D8.

### Receipt provenance

The canonical eval receipt binds the verdict to:

- root/session/goal identity and evaluated git state;
- producing workflow, iteration, attempt, and harness run;
- every recursively discovered regular check file and its eval-banana
  canonical definition hash copied from the raw report;
- judge provider, model, and reasoning effort;
- individual results and the final verdict;
- one canonical report plus its hash; and
- exactly one raw report from the producing attempt's trace plus its hash.

For a passing verdict, the engine also verifies the raw eval-banana report's
absolute project/output paths, all-pass threshold/status, zero judge exits,
check identities, and effective judge settings. It recaptures live HEAD and the
versioned dirty-tree digest before terminal acceptance. These are provenance
and transport checks; the coordinator does not reinterpret the LLM judge's
semantic reasons or add deterministic stock checks.

New sessions use iteration-local `goal_check.json` only as a projection of the
receipt. Verdict, reason, and receipt reference must agree. A projection or
receipt failure is recorded with field-qualified repair evidence and cannot
close the session.

### Identity-bound terminal control

Both v2 terminal signals must come from the exact session, workflow, and
attempt currently being completed. An older attempt, sibling session, or
spawned delegate cannot leave a signal for later work to apply.

`goal_met` must be produced by the workflow contract's terminal-control owner
and cite the matching passing same-session eval receipt. The D5 last-resort
`unresolvable_error` form instead requires an allowed current role, a specific
blocker, and routes already tried. It does not require an eval receipt:

```json
{
  "schema_version": 2,
  "control_id": "control-9",
  "state": "stopped",
  "reason": "The required deployment identity is unavailable after checking documented local and CI routes.",
  "stop_reason": "unresolvable_error",
  "producer": {
    "session_id": "session-delivery",
    "workflow_id": "inner",
    "attempt_id": "attempt-inner"
  },
  "attempted_routes": ["documented local identity", "existing CI identity"],
  "evidence_refs": ["session:/protocol_failures/deployment-blocker.json"],
  "created_at": "2026-07-15T12:00:00Z"
}
```

A spawned agent reports a blocker to its harness coordinator; the accountable
workflow role publishes the layer-owned signal. No paused or
waiting-for-human state exists.

Malformed v2 control is atomically archived under `control_rejected/` and
described under `protocol_failures/`; the session remains repairable until the
configured consecutive-protocol-failure cap is reached. The protocol version
comes from the frozen workflow contract, so rewriting mutable session files
cannot downgrade a live v2 attempt.

Exact receipt, projection, `goal_met`, and rejection schemas are defined in the
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
work can legitimately have those shapes. Parent and eval roles assess the
evidence and repair problems under D8.

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

Fresh sessions use protocol/state v2. Existing v1 trees resume in place:

- missing state/session schema means v1, and topology may be reconstructed from
  physical nesting and parent pointers;
- v1 child requests, goal checks, terminal control, `harness_outputs/`, raw
  eval directories, and markdown updates remain readable under a protocol-v1
  workflow contract;
- both legacy flat and v2 pending child-request inboxes are observed; and
- active legacy sessions are not relocated or retroactively assigned stronger
  provenance.

Packaged workflow contracts declare protocol v2 and treat v1 output as a
repairable protocol failure. Custom sets without a contract receive a
conservative derived v1 role contract, but a fresh tree still requires the v2
worker handshake.

The cross-repository contract is fail-fast:

- team-harness 0.5.0 supplies caller-owned run paths, input durability,
  direct-spawn envelopes/streams, nested caller context, and its capability
  API;
- eval-banana 0.3.5 supplies hermetic config selection, exact flat output,
  a public canonical check-definition digest, effective judge metadata, exact
  judge inputs, and collision-safe per-check artifacts; and
- loopy-loop 0.7.1 requires the advertised capabilities and returns HTTP 426
  before mutation when a fresh v2 tree meets an older worker.

Support packages are published before the loopy-loop version that consumes
them, so the loopy dependency lock resolves against public artifacts. Editable
sibling checkouts are only the coordinated-development bridge.

## Verification boundaries

The three repositories test the boundary they own:

- loopy covers recursive three-depth dispatch/unwind, stop/usage projection,
  assignment and reference confinement, child request/input freezing,
  receipt/control provenance and repair, recovery, git evidence, and trace
  finalization/integrity;
- team-harness covers caller-owned runs, pre-call coordinator input,
  direct-spawn assignments and canonical streams, nested caller identity, and
  structured process failures; and
- eval-banana covers hermetic execution, check hashes, flat raw output, and
  reported judge settings.

The coordinated release gate is formatting, lint, type checking, and the full
test suite in all three repositories.

## Independent implementation review

Claude Code and Antigravity independently reviewed the settled implementation and the
final bounded-shutdown follow-up. Both returned PASS with no remaining blocker. Their
evidence and adjudicated suggestions are recorded in the
[Claude Code review](../analysis/claude-code-recursive-loop-implementation-review.md)
and [Antigravity review](../analysis/antigravity-recursive-loop-implementation-review.md).
This establishes code readiness; it does not bypass the support-package publication,
lock refresh, and install-path CI sequence above.

## Alternatives rejected

**Fixed subagent graphs.** The coordinator sees the live problem and should
decide whether it needs research, implementation, review, debugging, or no
delegation.

**Treat every spawn as a loop layer.** Ephemeral tasks do not need durable goal,
recovery, and evaluation state. Only an explicit child request creates a layer.

**Separate one-, double-, and triple-loop schedulers.** They would duplicate
transition and recovery logic. One recursive node and edge express all depths.

**Persist only absolute paths.** Agents need absolute runtime capabilities, but
durable records must survive a moved checkout. Store logical identity and
render absolute paths per attempt.

**Enforce ownership with ACLs or scheduler vetoes.** D8 requires accountable
effects, evaluation, and repair rather than preventive fences.

**Let a child verdict close its parent.** The child and parent have different
goals; integration or release work may remain after a leaf passes.

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
  `pm_planner_dispatcher/`: layer-scoped eval and recursive planning prompts.

Cross-project anchors are team-harness's `caller_contract.py`, `harness.py`,
and `tools/agent_tools.py`, plus eval-banana's `cli.py` and `config.py`.

Existing crash/process behavior remains governed by
[`long-running-loop-reliability.md`](./long-running-loop-reliability.md), and
D3/D4 evaluation semantics by
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md).
