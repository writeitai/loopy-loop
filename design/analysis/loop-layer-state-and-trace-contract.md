# Loop-layer state, agent contract, and trace review

**Status:** architecture analysis; not a binding design or decision  
**Date:** 2026-07-15  
**Scope:** the current one-layer session, the planner/dispatcher two-layer
session tree, and a future three-layer session tree

This review asks whether every loop layer can retain its own progress,
decisions, evaluations, and delivery evidence; whether every coordinator and
spawned worker understands its place; and whether all relevant agent inputs,
outputs, and harness traces are retained with useful provenance.

Independent passes were also run with Claude Code and Antigravity:

- [Claude Code loop-contract review](./claude-code-loop-contract-review.md)
- [Antigravity loop-contract review](./antigravity-loop-contract-review.md)

Those are intentionally independent working notes. This document checks their
claims against the code and reconciles them with the binding decisions in
[`design/decisions.md`](../decisions.md).

## Executive verdict

The current two-level machinery is stronger than the current contract around
it.

The coordinator already has a credible depth-first stack machine: each session
has isolated state, an active-child pointer, attempt identity, worker identity,
atomic state writes, child crash-window reconciliation, and subtree usage
roll-up. The active-stack walkers are already iterative. This is a sound base.

It is not yet solid enough to enable grandchildren merely by removing the
two-level guard. The important missing pieces are identity, provenance, and
handoff contracts:

1. The complete team-harness trace is split between the loopy session and a
   global team-harness directory. With installed team-harness 0.4.0, this also
   breaks loopy's usage accounting.
2. A harness coordinator receives useful session paths, but a spawned
   Codex/Claude/Gemini worker receives no automatic loopy session, layer, goal,
   ownership, or state-path envelope.
3. A child request contains only a workflow set and free-form goal. The
   consumed input is deleted, and the resulting child record has no durable
   work-item identity, expected evidence contract, eval receipt, or git/PR
   handoff.
4. Evaluation evidence is session-local, which is good, but it is not bound to
   the exact goal, checks, attempt, harness run, or git revision it evaluated.
   The stock eval-banana path is also not runnable in a clean initialized
   target, and both `outer` and `eval_runner` can independently declare
   goal closure.
5. Prompt-directed planning/download scratch can land at the repo or filesystem
   root and is not added to an existing target's gitignore.
6. Semantic state and session-local worker traces share one ignored session
   tree and one retention policy, while the complete coordinator trace is
   split into a global team-harness directory. There is no self-contained,
   sealed trace bundle that a later cloud exporter could safely consume.
7. Role and path rules are still prompt conventions, and some packaged prompt
   text directly contradicts those rules.
8. The PM template's root goal describes how to dispatch work, not the actual
   project outcome the planner is supposed to drive.

The recommended direction is not a database, a more restrictive scheduler, or
parallel loopy workers. Keep files, one depth-first active path, strong
coordinators, and agent judgment. Add a small typed contract around that free
work:

- an immutable session manifest;
- an immutable per-attempt assignment envelope with absolute runtime paths;
- an automatically propagated spawned-worker envelope;
- structured child request, outcome, eval, and delivery receipts;
- a separate, self-contained trace plane;
- explicit recursive state invariants and three-depth recovery tests.

The engine should validate structure and record facts. It should not decide
whether a plan is wise, prevent an agent from editing a path, infer semantic
success from a process exit, or automatically accept a child on behalf of its
parent. That preserves D3, D4, D5, and D8.

## Use precise names for the three kinds of nesting

“Inner loop” currently means several different things. That ambiguity is
itself a contract problem.

| Concept | Meaning | Examples |
| --- | --- | --- |
| **Session layer** | One durable loopy session node with one scoped goal and its own state/evals | root PM session, child delivery session, future grandchild task session |
| **Workflow stage** | One scheduled role inside a session | `planner`, `dispatcher`, `outer`, `inner`, `eval_reviewer`, `eval_runner` |
| **Harness worker** | One ephemeral agent CLI spawned inside a team-harness run | Codex implementer, Claude reviewer, Gemini researcher |

Using session depth makes one/double/triple-loop topology unambiguous:

| Informal name | Durable session topology |
| --- | --- |
| one-loop | one session at depth 0; it may still contain several workflow stages |
| double-loop | root session at depth 0 and one active child at depth 1 |
| triple-loop | root at depth 0, child at depth 1, and grandchild at depth 2 |

The current `inner_outer_eval` workflow names do not create two durable loopy
layers. They are workflow stages sharing one session's goal and state.
`pm_planner_dispatcher` creates the second durable layer by requesting a
child session.

This distinction should appear in every schema, prompt, CLI status view, and
trace span.

## Current contract map

### Runtime actors

| Actor | Lifetime | Current authority | Current blind spot |
| --- | --- | --- | --- |
| loopy coordinator | whole run | mutates `state.json`, schedules one task, creates/finalizes children | does not know semantic progress, delivery facts, or complete harness trace |
| loopy worker | whole worker process | renders an assignment, invokes team-harness, persists normalized iteration artifacts | reconstructs paths locally; does not persist the exact task response as a typed assignment |
| team-harness coordinator | one loopy iteration | decomposes work, spawns and monitors workers, synthesizes a result | receives no typed layer contract; it gets a long prose prompt |
| spawned harness worker | one delegated task or resumed provider session | investigates or changes the checkout and writes artifacts | only knows loopy context that the harness coordinator happened to repeat |
| session workflow | many iterations | owns free-form semantic state under that session | ownership and expected outputs are prompt-only |
| eval workflow | one session layer | authors/runs that session's checks and emits evidence | result provenance is too thin; ancestor acceptance is not distinguished |
| target git repository | whole project | authoritative source/delivery history | observed branch/HEAD/PR relationship is not linked to attempts or child outcomes |

### State that is already strong

The following foundations should be preserved:

- `LoopState.current_task.attempt_id` rejects a late completion from a
  superseded dispatch.
- the worker's host, PID, and start-time token let the coordinator refuse a
  verifiably live duplicate worker;
- `_advance()` centralizes stop, child-dispatch, and next-workflow ordering;
- parent sessions retain `active_child_session_id` while suspended;
- `_reconstruct_session_stack()` walks to the deepest active node and
  reconciles several multi-file child-dispatch crash windows;
- each child gets its own `goal.md`, `state.json`, `project_state/`,
  `eval_checks/`, `control.json`, iterations, and harness output root;
- terminal child usage is rolled into the parent child record without
  double-counting;
- `events.jsonl` is correctly documented as a best-effort operational
  projection, not correctness state;
- mechanical iteration success remains separate from semantic acceptance
  (D3);
- one loopy assignment is active at a time even though one harness coordinator
  may parallelize independent subagents (D2).

These are meaningful strengths. The proposal below adds contracts around them
rather than replacing the state machine.

## High-priority findings

### P0 — the harness trace and usage contract is broken in the installed integration

The installed dependency is team-harness 0.4.0.

`TeamHarness.run()` creates its canonical run log at:

~~~text
~/.team-harness/runs/<run_id>/run.json
~~~

That record contains coordinator messages, tool calls and results, spawned
agent prompts and effective prompts, commands, model/effort choices, provider
session IDs, process identity, usage, retries, and timestamps. The relevant
implementation is team-harness `harness.py::TeamHarness.run` and
`tracking/run_log.py::RunLogWriter`.

The caller-supplied output directory instead receives:

~~~text
<session>/harness_outputs/<iteration>/<run_id>/
├── worker_sessions.json
└── workers/
    └── <worker>/
        ├── stdout.jsonl
        ├── stderr.log
        └── invocation/tail/exit artifacts
~~~

Team-harness does not copy `run.json` there.

There is also an input-persistence crash window. The loopy worker renders the
exact coordinator prompt, calls `run_harness_iteration()`, and only writes the
iteration's `prompt.txt` after that call returns. Team-harness creates an empty
`run.json` early, but constructs its generated system prompt and user message
in memory and records them only through a completed turn delta. A
configuration, provider, or process failure before the first logged turn can
therefore leave neither system's durable record containing the exact input
that was attempted.

Loopy should atomically persist its rendered prompt and assignment before
invoking team-harness. Team-harness should likewise persist the generated
coordinator system/user input envelope before its first model call, then append
turn deltas as execution proceeds. Sealed traces can mark a call as never
started or incomplete; they must not silently omit its intended input.

Loopy's `worker.py::_read_harness_usage()` nevertheless reads
`<harness_output_dir>/run.json`. Consequently, real successful iterations do
not find the usage record. They are recorded as usage unknown, and
`max_cost_usd` cannot reliably perform its intended job. The unit tests in
`src/tests/test_events_and_usage.py` construct a synthetic `run.json` in
the location loopy expects, so they do not test the real dependency contract.

`recovery.py::recover_interrupted_iteration()` already knows the other half
of the truth: it discovers run IDs in the session output tree, then opens
`team_harness.config.RUNS_DIR/<run_id>/run.json`. That means usage,
recovery, session inspection, and future export currently use inconsistent
location assumptions.

This is more than a metering bug:

- the session is not a self-contained audit bundle;
- the most complete coordinator and spawn-prompt ledger is outside the repo
  tree and outside loopy's documented retention boundary;
- a remote/shared-checkout worker cannot assume the coordinator host has the
  same global team-harness directory;
- a future exporter that walks only `.loopy_loop/sessions/` silently misses
  the coordinator trace and effective spawned-worker prompts.

Loopy's own linkage also stops one record too early. `IterationResult`
contains `harness_run_id` and `harness_output_dir`, but
`FinishedRequest` and `HistoryEntry` omit both. The iteration directory can
be scanned to recover the association, yet the durable scheduling history
does not directly name its trace. Add a compact trace reference/manifest ID to
the recovery/result receipt and history.

The clean contract is: the caller-supplied run output directory contains one
canonical, crash-durable run record, and the team-harness global directory is
at most an index or reference to it. Team-harness should return the exact run
record path as structured data; loopy should not infer it from a private
global. Recovery-critical process identity can also be mirrored into a compact
loopy recovery receipt so pruning bulk traces never breaks correctness.

An integration-contract test must use the real team-harness output layout
without making a model call. Fix this before relying on cost limits, trace
export, or triple-depth recovery.

### P0 — the stock eval path is not runnable on a clean initialized target

The Claude Code pass found and reproduced two independent dependency-contract
failures against the installed eval-banana 0.3.1.

First, the packaged `eval_reviewer/prompt.txt` teaches that
`target_paths` are resolved from the repo root. Eval-banana 0.3.1's
`HarnessJudgeCheckDefinition` forbids unknown fields and has no
`target_paths` field. A check that follows that guidance fails validation
with `extra_forbidden`.

Second, a `harness_judge` check needs a configured harness agent.
`loopy init` does not create `.eval-banana/config.toml`, the packaged
`eval_runner` command does not pass `--harness-agent`, and the README and
session-layout docs do not explain the prerequisite. A schema-correct check in
a clean initialized target therefore exits before judging.

Both defects fail closed: the eval runner should emit `goal_met: false`, so
they do not manufacture acceptance. They still make the recommended
`inner_outer_eval` template's only formal evidence channel unusable out of
the box.

There is also an identity gap. The judge agent/model is ambient eval-banana
configuration, not part of `RootConfigSnapshot`, the child inheritance
contract, the assignment envelope, or the eval receipt. An ancestor directory
can supply the config through eval-banana's upward search, so two otherwise
identical sessions may use different judges without recording why.

Fix the prompt/schema drift, ship or explicitly render the judge
configuration, snapshot its effective identity into the assignment/eval
receipt, and add a round-trip test:

~~~text
stock prompt-conformant check
  -> eval-banana's real loader/validator
  -> runnable harness_judge configuration
~~~

This preserves D4. It does not justify agent-authored deterministic checks.

### P0 — scratch and trace guidance can leak into the delivered product

The packaged prompts direct agents to `_feature_planning/`,
`/_feature_planning`, and `_additional_context/` while team-harness runs
at the target repo root. They explicitly encourage downloading external repos,
SDKs, examples, and docs there.

On a real target that already has a `.gitignore`, `loopy init` does not
replace it with the packaged template. `_ensure_gitignore()` appends only:

~~~text
.loopy_loop/sessions/
~~~

It does not ignore `_feature_planning/`, `_additional_context/`,
`_outputs/`, or the still-created root
`.loopy_loop/state.json.lock`. An implementation worker following the later
“branch, PR, and merge” instructions can include planning scratch or
downloaded third-party material in a product change, especially after
`git add -A`.

The primary fix is to give scratch one rendered, absolute home in the
per-attempt trace directory and remove all ambiguous repo-root paths from
prompts. Extending generated gitignore entries is useful defense-in-depth and
diff hygiene, not a preventive write fence. A workflow-set-owned diff check
can detect remaining scratch leakage and route it back for repair under D8.

### P0 — harness coordinators and spawned workers lack one shared assignment envelope

`worker.py::_render_prompt()` currently gives the team-harness coordinator:

- session, workflow-set, workflow, and iteration identity;
- goal and repo-level completion/stop criteria;
- session directory, goal, project state, eval checks, updates, child request,
  control, finished ledger, harness output, and iteration paths;
- a parent session directory when the session is nested.

Because the CLI passes `Path.cwd()`, these paths are normally absolute. This
is useful existing behavior.

It is not a complete contract:

- the prompt omits the root session ID, explicit parent session ID, depth,
  child request/work-item ID, attempt ID, layer purpose, state ownership,
  expected outputs, and git baseline;
- absolute paths are an implementation consequence, not a validated wire
  invariant or an artifact a worker can read programmatically;
- the repo root, `eval_results/`, `children.json`, and child sessions
  directory are not rendered even though stock workflows rely on them;
- the workflow body continues to use ambiguous relative names such as
  `project_state/current_state.md` and `children.json`, which resolve
  against the repo-root harness cwd if taken literally;
- a spawned worker gets only the coordinator-authored prompt plus
  team-harness's generic session-output footer. No loopy context is appended
  automatically.

The root `team_harness_system_prompt_extension` is inherited by every child.
The PM template already warns that PM-only role text there would contaminate
the child implementer. Treat that extension as tree-global invariants only.
Layer purpose and workflow role belong in the per-attempt envelope, where they
can differ without changing D9's uniform strong coordinator model.

The consequence is asymmetric understanding. The harness coordinator can
usually infer its session, while its implementer/researcher/reviewer may know
only “change this file” and a harness output directory. The trace proves what
prompt was sent after the fact, but nothing guarantees that prompt carried the
right ecosystem position.

The remedy is an immutable per-attempt `assignment.json`, generated by the
worker before team-harness starts. The harness coordinator prompt should lead
with its absolute path. Team-harness should automatically append a compact
loopy context footer to every spawned worker and record a per-worker
`agent_assignment.json`. Non-secret `LOOPY_*` environment variables can be
a convenience, but environment-only propagation is too implicit and too hard
to audit.

This is context propagation, not a permission system. It does not violate D8.

### P0 — parent-to-child input is lossy and child-to-parent output is inferential

`ChildSessionRequest` contains only:

~~~json
{
  "workflow_set": "inner_outer_eval",
  "goal": "Implement the selected item",
  "schema_version": 1
}
~~~

After dispatch, the request file is deleted. `children.json` retains its
filename and goal hash but not the exact request body. The goal itself is
copied into the child `goal.md` and the workflow set into child metadata, so
the two current semantic values survive; their original envelope and
provenance do not. There is no stable:

- request ID;
- parent work-item ID;
- producing parent attempt;
- acceptance criteria;
- input evidence list;
- expected output/eval/delivery contract;
- git baseline;
- retry/supersession link.

The dispatcher later scans child Markdown, eval directories, and git/PR prose
to infer which terminal child belongs to the selected item and what it
accomplished. A reused request filename is explicitly legal, which reinforces
that the filename cannot be semantic identity.

Child construction also reveals an important schema coupling. The coordinator
copies the parent's `RootConfigSnapshot` and changes only `goal`,
`goal_hash`, and `workflow_set`. Uniform provider/coordinator/model
configuration is deliberate under D9, but any non-empty parent
`completion_criteria` and `stop_criteria` also flow into the child prompt,
even though the child has a different scoped goal. Split the immutable
tree-wide execution profile from the session-local goal/acceptance contract;
inherit the former and derive the latter from the child request.

Accepted requests should be archived rather than deleted as the only copy.
Invalid requests are already moved to collision-safe `.rejected` files, which
is a good foundation, but their rejection reason exists only in operational
logs. A v2 request needs a request ID and explicit parent assignment, and both
accepted and rejected archives need a durable receipt that records the
disposition and reason.
When the child becomes terminal, the engine should generate a factual
`child_outcome.json` that links lifecycle, eval, delivery, trace, and usage
evidence. It must not say the parent work item is accepted; the parent planner
still makes that semantic decision and records its rationale separately.

### P0 — the PM scaffold describes the loop mechanism, not the target project

The packaged `pm_planner_dispatcher/loopy_loop_goal.txt` says only:

~~~text
Manage project work by selecting one concrete implementation item at a time,
dispatching it to a child implementation loop, and reviewing the child
evidence before accepting completion.
~~~

That is a description of the planner/dispatcher mechanism. It does not name a
project outcome, completion criteria, constraints, or the target's
authoritative plan. A clean initialization can therefore start a perfectly
functioning double loop whose top-level goal is merely to operate a double
loop. This undercuts D6's requirement that the planner drive the target's own
authoritative plan and makes root acceptance impossible to interpret.

The initializer cannot invent a user's project goal. It should make the PM
goal scaffold unmistakably incomplete, support supplying the real goal during
initialization, and document the required replacement as part of the normal
setup path. A preflight diagnostic may report that the unchanged scaffold is
still present, but this should not become a semantic scheduler veto or a
human-in-the-loop gate.

### P1 — state phases and whole-tree invariants are implicit

`LoopState` expresses lifecycle through a combination of `status`, three
stop booleans, `stop_reason`, `current_task`, and
`active_child_session_id`. Coordinator paths currently maintain the useful
combinations, but the model does not validate them.

The effective per-session phases are:

| Derived phase | Required shape |
| --- | --- |
| ready | running, no current task, no active child |
| executing | running, one current task, no active child |
| suspended | running, no current task, one active child |
| terminal | terminal status, no current task, no live child |

The whole tree adds one critical invariant:

> Exactly one live loopy assignment may exist in the entire session tree, and
> it belongs to the deepest active session.

These should be model/transition invariants with corruption diagnostics, not
facts future maintainers must re-derive from coordinator control flow. Add a
monotonic state revision and transition ID so receipts and trace manifests can
name the exact state projection they observed.

Two ownership checks should be tightened with the same work:

- the coordinator and worker do not exchange a stable repository identity.
  A worker started in the wrong checkout can receive a valid remote task,
  load same-named local workflow files, construct plausible local session
  paths, and post a completion whose artifacts are not in the coordinator's
  state tree. Include a repository ID/config hash in registration and the
  assignment handshake;
- an exact-coordinate `/finished` request is fenced by attempt ID, but the
  matching path does not also require its `FinishedRequest.worker` to equal
  the active task owner. Worker identity is optional on the request. Make the
  owner echo required for newly dispatched tasks and validate it on every
  completion, not only stale/mismatched calls.

Recovery should fail visibly when structural state is corrupt.
`_read_children_payload()` currently maps unreadable or invalid
`children.json` to an empty ledger, which can erase the evidence used to
avoid duplicate dispatch. `_adoptable_child_id()` can also find multiple
live children, adopt the newest, and leave the other for manual
reconciliation. Define deterministic autonomous reconciliation where it is
safe; otherwise preserve the corrupt inputs, record the exact terminal
`unresolvable_error`, and never make corruption look like “no children.”

`sessions.py::session_dir_path()` currently finds a nested session by
`rglob(session_id)` and returns the first sorted match. Deeper trees and
large trace directories make repeated lookup increasingly expensive, and a
copied session tree can make it ambiguous. Maintain a root/session ID to
canonical relative-path index (or derive from the validated pointer chain);
resolve that logical path to an absolute worker-local path in the assignment.

### P1 — triple depth is blocked in one place but underspecified in several places

`coordinator_app.py::_dispatch_child_session_if_requested()` immediately
returns when `state.parent_session_id is not None`. This is the direct
two-level guard.

The worker still renders that child session's absolute `child_requests/`
path. A child coordinator can therefore publish a valid-looking grandchild
request that is never scanned, rejected, logged, or surfaced as an event. It
remains on disk while ordinary workflows continue. Until deeper dispatch is
enabled, the runtime should render the capability as unavailable and turn any
such request into a visible rejected receipt with a reason, so an agent can
repair its plan instead of waiting forever.

Several other mechanisms are already recursive in shape:

- `_reconstruct_session_stack()` loops down active-child pointers;
- CLI status walks the same chain;
- nested physical session lookup uses the children tree;
- completion resumes one parent at a time, which can unwind multiple levels;
- child usage records can represent a subtree total.

That is encouraging, not proof that triple depth is complete. Before removing
the guard, specify and test:

- grandchild request identity and archival;
- crash windows on both parent-child edges;
- terminal unwind from depth 2 to depth 1, then eventually depth 0;
- root stop propagation while a grandchild is active;
- child-local `unresolvable_error` as evidence for its parent, not an
  automatic root failure;
- tree-wide cost/turn accounting while descendants are still active;
- events and status for every depth;
- stale `/finished` replay across a changing deepest session;
- root, intermediate, and leaf evaluation ownership.

“Triple loop” should not become a special branch. Generalize the same session
node and parent/child edge to arbitrary sequential depth. If a depth safety cap
is desired, make it an explicit frozen resource limit analogous to
`max_turns`, not a semantic rule about what work an agent is allowed to try.

### P1 — stop and budget semantics are not yet tree-wide

`loopy stop` mutates the latest top-level `StateStore`. If a child is
active, its session can continue until the parent resumes and observes the
root stop. At triple depth this delay can be very large.

The contract should distinguish:

- **session control:** a layer declares its own scoped goal met or its own
  blocker terminal;
- **tree control:** an operator/root request stops dispatch along the active
  descendant chain at the next safe scheduling boundary.

A child failure should return to the parent for autonomous repair or rerouting.
A root tree-stop should become visible to the deepest active layer without
requiring agents to write into another layer's `control.json`.

Cost accounting also needs a root-tree ledger updated on every completed
attempt, including attempts in an active descendant. Today a child's usage
only becomes part of a parent's child record when the child finalizes, and the
real run-location mismatch makes usage unknown in the first place. Fix trace
discovery first, then define whether turn and cost limits are per-session,
whole-tree, or both.

### P1 — eval evidence is in the right directory but lacks subject provenance

The core placement rule is already mostly correct: every child has its own
`eval_checks/`, eval results, per-iteration `goal_check.json`, and
`control.json`. An implementation child can therefore judge its scoped goal
without overwriting the PM parent's eval state.

`GoalCheckSignal` is only:

~~~json
{"goal_met": false, "reason": "...", "schema_version": 1}
~~~

It does not bind the verdict to:

- root/session ID and goal hash;
- workflow, iteration, and attempt;
- harness/eval run;
- exact check definitions or their hashes;
- judge model;
- target git commit and dirty-tree digest;
- supporting reports;
- producing actor.

Mutable check YAML also means an old verdict cannot prove which check prompt it
ran. `control.json` does not cite the evidence supporting a goal-met stop.

The one-layer template has two goal-met authorities with different evidence
bars. `eval_runner` is told to stop only after its checks pass. `outer` is
also told it may write `control.json` with `goal_met` after reviewing its
own accepted-completion ledger. The normal cadence lets `outer` run and stop
well before the first `eval_runner` assignment. A session can therefore
reach terminal `goal_met` with no eval receipt at all, even if the broken
eval setup above has never successfully run.

Do not solve this by making the scheduler semantically forbid an outer
workflow from acting. Give the stock set one unambiguous protocol instead:
`outer` owns task acceptance and records “ready for goal evaluation,” but it
never writes terminal `goal_met`; `eval_runner` is the sole goal-control owner
and writes that terminal decision only after producing the same-session eval
receipt. Prompt linting and workflow-set review can detect violations before a
template ships.

The current v1 runtime terminalizes as soon as it consumes a goal-met
`control.json`, so it cannot discover a missing receipt afterward and route
the session to repair. A future control schema should therefore cite the
same-session eval receipt as part of the terminal request. The engine may
validate the receipt's identity, existence, and subject hashes before applying
the state transition; that is structural protocol validation, not a second
semantic judgment. It must not reinterpret whether the LLM judge was right.

The PM parent has a second problem: the packaged
`pm_planner_dispatcher` set has planner and dispatcher stages but no explicit
parent-goal eval stage. Planner prose can mark the root goal met after reviewing
child evidence, but there is no independent parent-level integration judgment.

Use this invariant at every depth:

> A session evaluates only its own scoped goal. Descendant evidence is an
> input to an ancestor's evaluation, never a substitute for it.

The leaf evaluates its task. An intermediate feature layer evaluates the
integrated feature. The root evaluates the program/release goal. A PM template
should therefore gain a parent-scoped acceptance/eval stage before root
`goal_met`, even if that stage consumes child receipts.

This does not change D4. The stock generic eval remains `harness_judge`.
Target-owned deterministic tests may be additional evidence in a dedicated
target workflow set; do not make the stock evaluator author its own
deterministic checks.

### P1 — git, branch, PR, and merge state is prose-only

The packaged prompts ask agents to create branches, open PRs, wait for checks,
merge, and record URLs or blockers. Those facts live in
`current_state.md`/`finished.md` when the agent remembers to write them.
Neither the assignment nor the child record knows:

- starting branch and HEAD;
- ending branch and HEAD;
- dirty files before and after;
- intended base;
- PR URL/number and check state;
- merge commit;
- which repository a multi-repo change belongs to.

The engine should capture read-only `git_before.json` and `git_after.json`
observations at attempt boundaries. Compact identity, branch, HEAD, status
digest, and remote-fingerprint receipts belong in session state; verbose
status output and diffs belong in the trace plane. The workflow should write a
structured durable `delivery.json` for semantic claims such as PR intent, CI
status, merge status, and blockers. Parent/eval workflows compare the claimed
delivery with observed git state and repair inconsistencies.

Do not make dirty state or a missing PR an engine scheduling veto. Recording
facts and evaluating them later follows D8; automatic rejection in
`/finished` would not.

### P1 — several inputs and source versions are not immutable

The root goal is copied into `goal.md`, and each rendered coordinator prompt
is retained. Other inputs are weaker:

- `updates_from_user.md` is a mutable inbox that prompts may clear after
  incorporation, so the exact original update and its acknowledgement can
  disappear;
- accepted child request files are deleted;
- the coordinator caches workflow definitions at preflight, while the worker
  reloads `config.yaml` and `prompt.txt` from disk for each task;
- the rendered prompt preserves the workflow body, but there is no source hash
  connecting scheduler config, prompt text, and a coherent workflow-set
  version.

Use an append-only, addressed user-input journal with delivery/acknowledgement
records. Each update should name `target_session_id` or an explicit
`tree` scope; the deepest assignment should receive any still-undelivered
tree-scoped update instead of making a root update wait for the whole child
stack to unwind. Archive child requests. Snapshot or hash workflow
configuration and prompt sources per session/attempt so a mid-session edit
cannot silently make the coordinator schedule one definition while the worker
executes another.

### P1 — packaged prompt contradictions undermine the intended ownership model

The prompt contract should be concise enough that its important rules do not
compete with boilerplate. Current examples:

- `outer/prompt.txt` says the outer stage must not implement planned tasks,
  then a later generic block tells it to create an implementation team, execute
  the plan, create a PR, and merge it;
- `inner/prompt.txt` directs planning output to
  `/_feature_planning`, an absolute filesystem-root path, while other lines
  use relative `_feature_planning/` and describe it as session-local;
- generic blocks use `_additional_context/` without an unambiguous session
  path;
- prompts mention asking questions or a `questions.md` path despite D5's
  autonomous routing/terminal-stop contract;
- a large, model-specific team recipe obscures the actual stage goal and
  ownership rules.

Replace those blocks with a compact role, owned state, expected evidence, and
delegation outcome contract. Let the strong harness coordinator decide the
useful decomposition and choose configured model tiers, consistent with D9.
Add prompt linting for filesystem-root paths, stale placeholders, contradictory
role statements, and forbidden human-gate language.

## Adjudication of the parallel reviews

The three passes agree on the main direction: retain the depth-first file
architecture, formalize assignment/layer identity, make child handoffs typed,
bind evals to their subjects, observe git delivery, and separate traces from
semantic state.

Where they diverge, the code supports these conclusions:

- Antigravity's diagram and recovery recommendation say team-harness writes
  `run.json` under loopy's `harness_outputs/`. That is false for installed
  team-harness 0.4.0. Recovery must not be repointed there until the upstream
  output contract actually puts a canonical record there.
- Team-harness's current `_worker_log_paths()` does use the supplied
  `session_output_dir`, so spawned-worker stdout/stderr and
  `worker_sessions.json` are session-local. The missing global piece is the
  full coordinator `run.json`, not every worker stream.
- Do not adopt Antigravity's suggestion to reject dirty/divergent git in the
  coordinator. Capture git facts and let a workflow-set-owned acceptance check
  detect and repair delivery problems, as D8 requires.
- Do not adopt a mandatory dual gate in the stock generic eval template.
  D4 permits a target-owned deterministic suite in a specialized workflow set;
  it does not permit the generic eval author to invent deterministic checks.
- Do not stream traces fire-and-forget after every state mutation. Seal a
  complete local bundle, then use a durable asynchronous export outbox.
- Environment variables are a useful supplement, not the assignment source of
  truth. The immutable envelope and effective prompt provide the auditable
  contract.

The Claude Code pass additionally reproduced the eval-banana and gitignore
seam failures incorporated above. Its independent report contains a table
placing agent stdout/stderr in the global run directory; for loopy's supplied
`output_dir` path, the installed team-harness code places those streams in
the session output instead. This does not affect its headline finding about
the missing coordinator run record.

## The recommended two-world model

From a user and agent perspective there should be two worlds.

### 1. State and evidence

Small, durable, inspectable artifacts needed to decide what happens next:

- engine scheduling state and recovery receipts;
- immutable goal and assignment identity;
- progress, plans, meaningful decisions, and accepted work;
- child request/outcome/acceptance receipts;
- eval definitions and compact verdict receipts, including the rationale
  needed to audit acceptance;
- compact git/delivery receipts needed to identify what was delivered;
- trace references and hashes.

Within this world, distinguish semantic state from the recovery journal:

- **semantic state** is written by workflow roles and explains what/why;
- **recovery state** is written by the engine and proves which assignment and
  processes were active.

Both are correctness-critical. A trace exporter outage or trace-retention
cleanup must not make the session impossible to resume.

### 2. Traces and logs

Exhaustive, append-only execution records that explain how:

- exact coordinator system/user prompts and visible responses;
- model turns, compaction records, tool calls, arguments, and results;
- coordinator-authored and effective spawned-agent prompts;
- commands, model/effort, provider session IDs, and process identity;
- stdout/stderr and provider CLI event streams;
- loopy worker/coordinator operational logs and HTTP envelopes;
- raw eval output;
- git snapshots/diffs;
- timing, retries, failures, usage, and completeness flags.

Traces are separately ignored, prunable after a safe retention point, and
exportable. Trace loss is unfortunate but must not change semantic acceptance
or scheduling. No correctness-critical evidence may exist only behind a
prunable trace reference: compact eval, git, and delivery facts stay in the
state/evidence plane, while verbose reports, command output, and diffs may live
only in traces.

### Proposed physical boundary

Keep the current nested session tree for compatibility and introduce a
separate trace root:

~~~text
.loopy_loop/
├── sessions/
│   └── <root_session_id>/
│       ├── session.json
│       ├── state.json
│       ├── control.json
│       ├── project_state/
│       ├── inputs/
│       │   └── user_updates.jsonl
│       ├── child_requests/
│       │   ├── pending/
│       │   ├── accepted/
│       │   └── rejected/
│       ├── child_outcomes/
│       ├── eval_checks/
│       ├── eval_receipts/
│       ├── git_receipts/
│       ├── delivery_receipts/
│       ├── iterations/
│       │   └── <iteration>_<workflow>/
│       │       ├── assignment.json
│       │       ├── prompt.txt
│       │       ├── result.json
│       │       ├── goal_check.json
│       │       └── trace_ref.json
│       └── children/<child_session_id>/...
└── traces/
    └── <root_session_id>/
        └── sessions/<session_id>/
            └── attempts/<attempt_id>/
                ├── trace_manifest.json
                ├── protocol/
                ├── harness/
                │   ├── run.json
                │   └── worker_sessions.json
                ├── agents/<agent_id>/
                ├── eval/
                ├── git/
                └── service/
~~~

Add `.loopy_loop/traces/` to `.gitignore` independently. Keeping
`prompt.txt` and the normalized result receipt under the session preserves
D1's current promise that the exact loopy assignment and result remain with
the durable session. Bulk model/process telemetry moves to the trace plane.

The semantic session tree may remain ignored by the target repo by default.
Automatically committing rapidly changing runtime state onto implementation
branches would pollute PRs and create conflicts. If remote durability is
needed, add an explicit session checkpoint/export mechanism rather than
silently versioning runtime state with product changes.

## Contract bundle

### Immutable session manifest

Evolve `session.json` from basic metadata to stable topology and origin:

~~~json
{
  "schema_version": 2,
  "session_id": "session-child",
  "root_session_id": "session-root",
  "parent_session_id": "session-root",
  "depth": 1,
  "workflow_set": "inner_outer_eval",
  "layer_kind": "delivery",
  "goal_hash": "abc123",
  "origin": {
    "request_id": "req-42",
    "parent_attempt_id": "attempt-parent",
    "parent_work_item_id": "WP-3"
  },
  "created_at": "..."
}
~~~

Canonical durable references should be IDs or repo-relative paths. Persisted
absolute paths become stale when a checkout moves. Runtime assignment
manifests should resolve those references to absolute paths on the worker host.

Legacy sessions can derive `root_session_id` and depth by following
`parent.json` links.

### Workflow-set contract

Each workflow set should ship a small declarative contract copied or hashed
into the session. It describes:

- the layer purpose;
- workflow roles and their responsibilities;
- intended readers/writers of semantic state artifacts;
- where meaningful decisions live;
- eval author, runner, and goal-control roles;
- whether/how the layer requests a child;
- expected assignment outputs and evidence.

Example shape:

~~~yaml
schema_version: 1
layer_kind: delivery
state:
  - path: project_state/current_state.md
    owner_roles: [outer, inner, eval_runner]
  - path: project_state/decisions.md
    owner_roles: [outer]
  - path: project_state/finished.md
    owner_roles: [outer]
eval:
  author_role: eval_reviewer
  runner_role: eval_runner
  goal_control_role: eval_runner
child_interface: none
~~~

This is instruction and audit metadata, not an ACL. The engine can validate
that the contract is structurally well formed and render it consistently.
Workflow-set-owned evals may detect ownership violations from state-effect
snapshots and give an agent a repair path. The engine should not block a write
before it occurs.

### Per-attempt assignment envelope

Before invoking team-harness, the loopy worker should atomically write
`assignment.json`. Suggested fields:

~~~json
{
  "schema_version": 1,
  "identity": {
    "root_session_id": "session-root",
    "session_id": "session-child",
    "parent_session_id": "session-root",
    "depth": 1,
    "request_id": "req-42",
    "work_item_id": "WP-3",
    "workflow_set": "inner_outer_eval",
    "workflow_id": "inner",
    "iteration": 4,
    "attempt_id": "attempt-abc"
  },
  "actor": {
    "kind": "harness_coordinator",
    "role": "inner",
    "layer_kind": "delivery"
  },
  "objective": {
    "goal_ref": "session:goal.md",
    "goal_hash": "abc123",
    "assignment": "Implement the selected leaf task",
    "completion_criteria": [],
    "expected_outputs": ["state update", "delivery receipt", "verification evidence"]
  },
  "paths": {
    "repo_root": {"ref": "repo:/", "absolute": "/abs/repo"},
    "session_root": {"ref": "session:/", "absolute": "/abs/repo/.loopy_loop/sessions/..."},
    "parent_session_root": {"ref": "parent:/", "absolute": "/abs/repo/.loopy_loop/sessions/root"},
    "project_state": {"ref": "session:project_state/", "absolute": "/abs/.../project_state"},
    "eval_checks": {"ref": "session:eval_checks/", "absolute": "/abs/.../eval_checks"},
    "eval_results": {"ref": "session:eval_results/", "absolute": "/abs/.../eval_results"},
    "children_index": {"ref": "session:children.json", "absolute": "/abs/.../children.json"},
    "children_root": {"ref": "session:children/", "absolute": "/abs/.../children"},
    "child_requests": {"ref": "session:child_requests/", "absolute": "/abs/.../child_requests"},
    "user_inputs": {"ref": "session:inputs/user_updates.jsonl", "absolute": "/abs/.../inputs/user_updates.jsonl"},
    "control": {"ref": "session:control.json", "absolute": "/abs/.../control.json"},
    "trace_root": {"ref": "trace:/", "absolute": "/abs/repo/.loopy_loop/traces/..."}
  },
  "ownership": {
    "own_session_state": "write according to workflow role",
    "parent_session_state": "read/reference; communicate through receipts",
    "engine_state": "read only",
    "trace_state": "write artifacts; never use as semantic truth"
  },
  "provenance": {
    "workflow_config_sha256": "...",
    "workflow_prompt_sha256": "...",
    "root_config_sha256": "...",
    "git_before_ref": "session:git_receipts/git-before-attempt-abc.json"
  }
}
~~~

The prompt should repeat the short human-readable identity and role, but direct
the coordinator to the absolute assignment path rather than duplicating a
large, error-prone list of path rules.

Resolve `repo_root` with `Path.cwd().resolve()` and assert absolute paths in
tests. The absolute value is the operational contract; the logical reference
is the portable contract.

### Spawned-worker delegation envelope

Team-harness should automatically preserve the parent assignment context for
every spawn. Extend spawn metadata with optional:

- `logical_actor_id`;
- `role`;
- `task_id`;
- `expected_outputs`;
- `state_responsibility`.

For each worker, persist `agent_assignment.json` and append an effective
prompt footer stating:

- it is an ephemeral spawned worker, not the loopy session coordinator;
- root/session/depth and parent harness-run identity;
- its coordinator-assigned role and scoped task;
- the absolute loopy assignment path;
- the exact output directory;
- whether it should change semantic state or return evidence to the
  coordinator;
- expected result/evidence.

The harness already records coordinator-authored `prompt`, effective
`full_prompt`, command, cwd, stdout/stderr, provider session, and
requested/effective model/effort. Preserve those records in the self-contained
trace bundle.

Default to one semantic-state committer per harness assignment: the harness
coordinator synthesizes worker evidence and updates layer state, or explicitly
delegates one file to one state-steward worker. Other parallel workers write
unique trace artifacts. This is a coordination convention, not a filesystem
lock.

### Subagent session catalog

`worker_sessions.json` already carries provider session IDs and resume
capability. Later loopy iterations cannot easily discover a useful earlier
worker by logical role.

Maintain a compact session-level catalog:

~~~text
logical_actor_id -> harness run -> agent id -> provider session id
                 -> role/task -> last output/evidence -> resume mode
~~~

A later coordinator may resume a relevant provider session through
team-harness. Resumption is an optimization for context and efficiency, never
the durable state mechanism. If the provider session is gone, the next agent
must reconstruct from session files.

### Child request v2

A child request should be a stable assignment, not just a goal string:

~~~json
{
  "schema_version": 2,
  "request_id": "req-42",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "attempt-parent",
    "parent_work_item_id": "WP-3",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Implement WP-3",
    "completion_criteria": ["..."],
    "constraints": ["..."],
    "deliverables": ["..."],
    "required_evidence": ["child eval receipt", "delivery receipt"]
  },
  "inputs": [
    {"ref": "parent:project_state/work_items.md", "sha256": "..."}
  ],
  "delivery": {
    "pr_expected": true,
    "merge_expected": true
  }
}
~~~

The containing parent and creation timestamp are engine-derived. Publish via
temp-file-plus-rename as today, then atomically move accepted/rejected requests
into their archive directories. Persist the acceptance or rejection reason in
a receipt beside the original request. A request ID, not a filename, provides
idempotency.

### Child outcome and parent acceptance

When a child is terminal, the engine should produce a factual
`child_outcome.json`:

~~~json
{
  "schema_version": 1,
  "request_id": "req-42",
  "child_session_id": "session-child",
  "goal_hash": "abc123",
  "lifecycle": {
    "status": "goal_met",
    "stop_reason": "goal_met",
    "started_at": "...",
    "completed_at": "..."
  },
  "evidence_refs": {
    "handoff": "child:project_state/handoff.json",
    "eval": "child:eval_receipts/eval-7.json",
    "delivery": "child:delivery_receipts/delivery-7.json",
    "git": "child:git_receipts/git-after.json"
  },
  "trace_ref": "trace:trace_manifest.json",
  "usage": {},
  "completeness": {
    "eval_receipt_present": true,
    "delivery_receipt_present": true,
    "trace_sealed": true
  }
}
~~~

The engine reports existence and lifecycle facts, not “work is good.” A parent
planner writes a separate acceptance record with:

- parent work-item/request ID;
- accepted, rework, or reroute disposition;
- evidence reviewed;
- rationale;
- follow-up request/supersession if any.

This preserves D3 and prevents a child's scoped `goal_met` from silently
closing its ancestor's goal.

### Decision records

Current `decisions.md` files are useful human-readable state but lack stable
identity and producer provenance. Keep Markdown if it works for agents, and
add either one immutable JSON record per meaningful decision or a structured
append-only index with:

- decision ID and subject;
- producing session/workflow/attempt/actor;
- decision and rationale;
- alternatives considered;
- evidence references;
- `supersedes` relationship;
- timestamp.

Do not record every tactical command as a “decision.” Raw execution belongs in
the trace.

## Evaluation contract

### Ownership at every layer

| Layer | What its eval can establish | What it cannot establish |
| --- | --- | --- |
| leaf delivery/task | the child-scoped goal at a named repo revision | parent feature/program acceptance |
| intermediate feature/epic | integration of its children into the feature goal | root program/release completion |
| root PM/program | the root goal using descendant and integration evidence | nothing above it |

Within a session, keep separate roles for:

- check/policy author;
- check runner;
- task-acceptance owner;
- terminal goal-control owner.

They may be workflow stages rather than different models, but the assignment
contract must state who owns which output. In the stock one-layer protocol,
`outer` owns task acceptance and `eval_runner` owns terminal goal control.

### Eval receipt

Keep raw eval-banana output in the trace/eval area and a compact
correctness-relevant receipt with the session:

~~~json
{
  "schema_version": 1,
  "eval_id": "eval-7",
  "subject": {
    "root_session_id": "session-root",
    "session_id": "session-child",
    "goal_hash": "abc123",
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
    {"check_id": "goal_outcome", "sha256": "...", "kind": "harness_judge"}
  ],
  "judge": {"provider": "...", "model": "..."},
  "verdict": {"goal_met": false, "reason": "..."},
  "check_results": [
    {"check_id": "goal_outcome", "passed": false, "reason": "..."}
  ],
  "canonical_report_ref": "session:eval_receipts/eval-7.report.md",
  "raw_report_refs": ["trace:eval/report.json"],
  "created_at": "..."
}
~~~

The receipt, its compact check results, and the canonical report are durable
state/evidence. `raw_report_refs` points only to optional verbose material; its
later pruning cannot invalidate the recorded verdict.

`goal_check.json` can remain the scheduler's small signal, but it should cite
the eval receipt and require an explicit schema version (the generic signal
reader already rejects a non-1 value, while the model currently defaults a
missing value to 1). A future `control.json`
version should cite the decision/eval receipts supporting a terminal stop.
The coordinator validates identity, existence, and hashes; it does not
reinterpret the LLM judgment.

For high-stakes completion, consider an independently produced second
layer-level receipt or a parent acceptance stage. This is an eval-policy choice,
not a reinterpretation of `IterationResult.success`.

## Git and delivery contract

Capture two complementary views.

### Engine-observed git snapshots

At dispatch and finish, record for each known repo:

- absolute and logical repo identity;
- branch/detached state;
- HEAD;
- porcelain status and a stable dirty-tree digest;
- sanitized remote fingerprints;
- timestamp and attempt.

This records facts without altering git or deciding what they mean.

### Workflow-authored delivery receipt

The implementation/outer workflow records:

- intended base and work branch;
- changed repositories;
- commits;
- PR URL/number;
- CI/check status and evidence timestamp;
- merge status and merge commit;
- explicit blocker and remaining action.

Parent and eval workflows compare this receipt with the observed snapshots and
repair discrepancies. The loopy engine should not refuse a completed
assignment merely because the tree is dirty or no PR exists: research,
planning, eval-only, local, and blocked work can all be legitimate. Delivery
quality remains an acceptance check.

Because parent and child are depth-first on one checkout, the parent sees the
child's final git state when it resumes. The structured child outcome removes
the need to rediscover which commits and PRs belong to which request.

## Complete input/output accounting

“All agent I/O” should mean every logical input delivered to a model/CLI and
every visible output/effect the system can observe. It cannot and should not
mean hidden chain-of-thought, provider-internal bytes, or raw credentials.

### Inputs to retain

- root goal and every child assignment;
- append-only user updates with target scope and acknowledgement;
- frozen root config plus workflow config/prompt hashes;
- exact loopy `TaskResponse`/assignment envelope;
- exact team-harness coordinator system and user prompts;
- every coordinator-to-worker prompt and effective appended footer;
- model, effort, cwd, non-secret runtime identity, and dependency versions;
- input artifact references/hashes;
- git-before snapshot.

### Outputs to retain

- visible coordinator responses;
- tool calls, arguments, results, retries, and compaction summaries;
- worker stdout/stderr and CLI event streams;
- provider session IDs and exit/process outcomes;
- normalized iteration result and exact `/finished` request/response;
- semantic state effects and file hashes before/after;
- git-after snapshot and delivery receipt;
- eval receipt and raw reports;
- child outcome/parent acceptance;
- service/recovery events and salvage reports.

The current system captures much of this across multiple places. The trace
manifest's job is to join it, state what is missing, and make completeness
machine-readable.

## Trace manifest and cloud export

Every attempt should have one `trace_manifest.json` with:

- schema version and lifecycle (`active`, `sealed`, `incomplete`);
- root/session/request/work-item/workflow/iteration/attempt/harness IDs;
- parent trace/span identity;
- runtime and package versions;
- artifact path, logical type, media type, byte size, and SHA-256;
- sensitivity classification;
- whether content is raw, redacted, truncated, or unavailable;
- timestamps and usage;
- export eligibility.

The natural trace hierarchy is:

~~~text
root session
└── session layer
    └── loopy attempt
        └── harness coordinator run
            └── spawned worker
                └── provider/tool/process events
~~~

### Export behavior

Future cloud export should be:

- opt-in in frozen run configuration, not an interactive approval step;
- local-first: write and seal the trace before upload;
- asynchronous through a durable outbox, never a fire-and-forget request in
  the coordinator state mutation;
- idempotent and content-addressed;
- compressed and encrypted in transit/at rest;
- redaction-aware;
- able to export metadata-only, redacted, or encrypted-raw policy tiers;
- resilient: an unavailable analysis cloud must not block loop progress;
- explicit about retention and deletion after a verified upload.

Prompts, tool arguments, shell output, commands, source code, user updates, and
environment values can contain credentials, proprietary code, PII, or
customer data. Do not upload a raw team-harness `run.json` blindly.
Team-harness currently records spawn tool arguments and full prompts; its
invocation artifact applies some command redaction, but that is not a complete
cloud-safety boundary.

Absolute local paths should remain in the local raw bundle for diagnosis. The
exported view should also carry logical references and may normalize local
path prefixes.

## Recursive state-machine contract

Use one session schema and one parent/child edge at every depth.

### Per-node transitions

~~~text
ready
  -> executing                    dispatch workflow attempt
executing
  -> ready                        record result; more local work
  -> terminal                     apply session control/resource stop
ready
  -> suspended                    accept and start one child request
suspended
  -> ready                        child terminal; write child outcome; resume
~~~

A terminal node cannot have a live task or live child. A suspended node cannot
have a current task. A child must point back to the exact parent/request that
points to it.

### Whole-tree rules

1. There is one active root-to-leaf path.
2. There is at most one live loopy assignment in the tree.
3. Only the deepest live node may own that assignment.
4. Every ancestor of that node is suspended and points to the next node.
5. Child terminal state is finalized into an outcome before the parent
   resumes.
6. Parent acceptance is a later semantic transition, not part of child
   finalization.
7. Root tree-stop becomes visible down the active path at a safe boundary.
8. Resource accounting includes every completed attempt in the tree.
9. Every edge and attempt has stable IDs, revisions, and trace references.

### Three-depth example

~~~text
depth 0: program planner
  goal: complete the release
  state: roadmap, cross-feature decisions, release eval
  suspended on request FEATURE-4

depth 1: feature planner
  goal: complete FEATURE-4
  state: feature tasks, feature decisions, integration eval
  suspended on request TASK-4.2

depth 2: delivery session
  goal: complete TASK-4.2
  state: implementation plan, work evidence, task eval, delivery receipt
  executing one harness attempt
~~~

When depth 2 stops, depth 1 reviews its outcome and may dispatch another task.
Only after depth 1 evaluates the integrated feature can it stop and return a
feature outcome to depth 0. Depth 0 then performs root-level review/evaluation.

No layer reads a descendant's `goal_met` as proof of its own goal.

## Recommended implementation sequence

### P0 — make the present contract truthful

1. Define one canonical team-harness run-record location under the supplied
   output root; persist its generated input envelope before the first model
   call; return the record path explicitly; and fix loopy usage discovery and
   recovery against that contract.
2. Add a real integration-contract test that would catch the current 0.4.0
   layout mismatch.
3. Repair the stock eval-banana contract: remove stale `target_paths`
   guidance, provide an explicit harness-judge configuration, record the
   effective judge, and validate a prompt-conformant check through
   eval-banana's real loader in CI.
4. Give scratch one session/attempt-local absolute path; extend generated
   gitignore hygiene for scratch, traces, and the root state lock.
5. Add `session.json` v2 and immutable per-attempt `assignment.json`,
   including attempt ID, role/layer identity, source hashes, and absolute
   runtime paths. Atomically persist the rendered `prompt.txt` before invoking
   the harness, not only after it returns.
6. Automatically propagate the loopy envelope into every spawned-worker
   effective prompt and trace record.
7. Archive child requests and add request/work-item/parent-attempt identity.
8. Replace the PM template's mechanism-only goal with an unmistakable
   target-goal placeholder and support supplying the real goal at init time.
9. Remove the contradictory generic blocks and filesystem-root paths from the
   packaged prompts; add prompt linting.
10. Snapshot/hash workflow sources so preflight and execution cannot silently
   disagree.

### P1 — make layer handoffs and recursion reliable

1. Add explicit `LoopState` shape invariants, state revisions, fail-visible
   child-ledger corruption handling, exact worker ownership, and a repository
   handshake.
2. Add child outcome and parent acceptance receipts.
3. Add eval provenance receipts, make one-layer goal closure evidence-bound,
   and add a parent/root acceptance-eval stage to the PM workflow set.
4. Add observed git snapshots and workflow-authored delivery receipts.
5. Add append-only user inputs with target scope and acknowledgements.
6. Add a session catalog for logical spawned actors/provider sessions.
7. Make root tree-stop and tree-wide resource accounting visible at the
   deepest active node.
8. Test all depth-2 dispatch, crash, recovery, stale-result, stop, event,
   usage, and unwind transitions.
9. Only then remove the current child-of-child dispatch guard.

### P2 — make traces operational products

1. Introduce the separate trace root and sealed manifest.
2. Capture loopy protocol/service logs and state/git effects.
3. Add trace inspection/pruning commands with active-run safety.
4. Add redaction and secret-leak tests.
5. Add a durable, idempotent cloud export outbox.

## Alternatives considered

### Put everything into one global event-sourced database

Rejected for now. A database can make multi-record transactions easier, but it
does not solve role clarity, eval subject identity, or evidence quality. It
would weaken the inspectable file model in D1 and add migration/availability
complexity before the file contract is even defined.

### Flatten every session into one directory and relate it only by IDs

Plausible later, but not required. It simplifies arbitrary-depth lookup and
portable tooling. It also creates a large migration and loses the useful
physical containment of child evidence. Keep nested sessions now, add stable
IDs/logical references, and revisit if lookup performance or remote storage
requires it.

### Commit semantic runtime state to the target branch

Not by default. It would make state remote and diffable, but frequent
coordinator writes would contaminate implementation PRs and create branch
conflicts. Keep code/delivery truth in git and session state in durable files;
offer explicit checkpoint/export if remote persistence is needed.

### Pass only environment variables

Insufficient. Environment variables are convenient for tools, but invisible
in ordinary artifact inspection and easy to lose across process boundaries.
Use them only as aliases for an immutable assignment file whose path also
appears in the effective prompt and trace.

### Enforce state ownership with path ACLs or scheduler vetoes

Rejected by D8. Publish intended ownership, record state effects, and make
violations visible to layer-specific evals with a repair path.

### Treat every green worker exit as success

Rejected by D3. Worker exit and harness mechanics are trace facts. Quality
comes from layer-scoped eval and acceptance receipts.

### Stream traces directly to the cloud instead of retaining locally

Rejected. It adds a network dependency to correctness, loses data during
outages, complicates recovery, and conflicts with the requested complete local
record. Seal locally, then export asynchronously.

### Add special one-loop, double-loop, and triple-loop code paths

Rejected. The same session node and parent/child edge should recurse.
Topology-specific workflow sets define the semantics; the engine provides one
depth-first stack machine.

## Verification checklist for a future binding design

- real team-harness run output contains or explicitly links one canonical
  complete run record;
- usage/cost accounting is exercised against that real layout;
- every attempt has immutable session/layer/workflow/attempt identity;
- exact loopy and generated harness input prompts are durable before their
  first model call;
- every path presented to a coordinator or worker is absolute and maps to a
  stable logical reference;
- every spawned worker automatically receives its loopy position, task, state
  responsibility, and output path;
- exact user updates and child requests survive consumption;
- workflow source hashes prove what scheduler config and prompt executed;
- child outcome never implies parent acceptance;
- every eval names its goal hash, checks, attempt, report, judge, and git
  subject;
- parent/root goal completion has its own layer-scoped eval/acceptance;
- git/PR claims are linked to observed repository facts without engine vetoes;
- three-depth happy path, crash windows, recovery, stop propagation, and unwind
  are tested;
- trace deletion cannot break resume or acceptance;
- trace export is idempotent, redacted according to policy, and failure-safe;
- legacy sessions remain readable and are not moved while active.

## Verification performed for this analysis

- inspected the current repo at `main` / `a5f9933`;
- inspected installed team-harness 0.4.0 source, including
  `TeamHarness.run`, `RunLogWriter`, worker log placement, spawned-agent
  records, and recovery integration;
- inspected eval-banana 0.3.1's real `HarnessJudgeCheckDefinition` schema;
- Claude Code reproduced the stale `target_paths` validation failure, the
  missing harness-agent configuration failure, and target gitignore behavior
  in clean temporary repos;
- ran the full loopy test suite: **216 passed**.

The green tests establish that the implemented state/recovery behavior still
matches its unit tests. They do not contradict the integration findings:
current usage tests create `run.json` at loopy's expected path rather than
exercising team-harness's actual producer layout, and current template tests
do not round-trip prompt-conformant eval checks through eval-banana.

## Decision impact if this direction is accepted

This file is analysis, so it does not change the current decisions.

A later binding design should likely:

- refine D1 to name the state/evidence plane and separate trace plane while
  retaining exact assignment/result receipts with the session;
- refine D6 from a specifically two-layer PM/child mechanism to the same
  depth-first session edge at additional configured depths;
- refine D7/team-harness's companion decision so the canonical run/recovery
  record has an explicit caller-visible location;
- preserve D2, D3, D4, D5, D8, and D9 unchanged in substance.

This analysis produced the accepted target design at
`design/designs/recursive-loop-layer-contract.md` after the state/trace boundary
and team-harness ownership model were agreed.
