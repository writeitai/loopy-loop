---
name: loopy-loop
description: Set up, configure, run, monitor, resume, or extend loopy-loop protocol-v3 workflows. Use for standalone inner/outer loops, planner/dispatcher recursive loops, durable layer state and handoffs, optional evaluation, dynamic Team Harness delegation, workflow cadence, child sessions, traces, and coordinator/worker operation.
---

# loopy-loop

`loopy-loop` drives long-running AI work inside a repository. A FastAPI
coordinator owns durable session and scheduler state; exactly one worker runs
each selected workflow through `team-harness`. Continuity lives in files and
git, never in a chat transcript.

Protocol v3 gives every durable loop layer one accountable orchestrator. That
role owns the layer plan, accepts work, maintains the upward handoff, and
decides when the layer goal is complete. Evaluation roles provide optional
evidence; they never own completion or impose a mandatory gate.

Use this skill to:

- initialize or configure a target repository;
- author a workflow set or scheduling cadence;
- run a standalone `inner_outer_eval` loop;
- run a recursive `pm_planner_dispatcher` parent with delivery children;
- operate or recover the coordinator and worker; or
- inspect assignments, visible semantic state, outcomes, and traces.

## Install and initialize

Install for a target repository:

```bash
uv tool install loopy-loop
```

Install a development checkout:

```bash
uv sync --all-extras
```

Initialize from the target repository root:

```bash
loopy init --template inner_outer_eval
loopy init --template pm_planner_dispatcher
```

`loopy init` is additive: it does not overwrite existing workflow files. The
PM template also installs `inner_outer_eval`, because the dispatcher normally
uses it for child delivery sessions.

The target repository contains:

```text
loopy_loop_config.yaml
loopy_loop_goal.txt
.loopy_loop/workflow_sets/<set>/
├── contract.yaml
└── workflows/<role>/
    ├── config.yaml
    └── prompt.txt
```

Runtime session state and traces are gitignored.

## Configure the root

`goal_file`, `workflow_set`, and `max_turns` are required. The goal text is
frozen into each session; do not use an inline `goal` key.

```yaml
goal_file: loopy_loop_goal.txt
workflow_set: inner_outer_eval
max_turns: 160

team_harness_provider: codex
team_harness_model: <coordinator-model>
team_harness_agents: [codex, claude, gemini]

model_tiers:
  frontier:
    codex: {model: <model>, effort: <effort>}
    claude: {model: <model>, effort: <effort>}
    gemini: {model: <model>, effort: <effort>}
  strong:
    codex: {model: <model>, effort: <effort>}
    claude: {model: <model>, effort: <effort>}
    gemini: {model: <model>, effort: <effort>}
  standard:
    codex: {model: <model>, effort: <effort>}
    claude: {model: <model>, effort: <effort>}
    gemini: {model: <model>, effort: <effort>}
  economy:
    codex: {model: <model>, effort: <effort>}
    claude: {model: <model>, effort: <effort>}
    gemini: {model: <model>, effort: <effort>}
default_tier: standard

team_harness_api_base: https://openrouter.ai/api/v1
team_harness_api_key_env: OPENROUTER_API_KEY
team_harness_system_prompt_extension: ""
```

Optional Grok Build worker family (team-harness ≥ 0.6.1): when `grok` is
installed and authenticated (`XAI_API_KEY` or `grok login`), append `grok` to
`team_harness_agents` and add model/effort cells (e.g. `grok-4.5` / `high`).
Do not list families the worker host cannot run — the capability roster treats
every listed agent with a model cell as available.

The four canonical strength tiers are:

- `frontier`: hardest architecture, ambiguous debugging, adversarial review,
  eval-policy/check design, and high-stakes judging;
- `strong`: complex implementation, reasoning, and review;
- `standard`: balanced ordinary implementation and analysis; and
- `economy`: bounded reconnaissance, formatting, and low-risk mechanical work.

The generated `harness_capability_roster.json` shows every enabled family and
every configured or unavailable tier cell. Stock prompts select from that
roster instead of naming vendors or models. For nested spawns, pass the chosen
family, model, and configured effort explicitly; do not guess unavailable
bundles.

`team_harness_system_prompt_extension` reaches every workflow set and every
session depth. Keep it layer-neutral. Role-specific instructions belong in the
workflow prompt and frozen Assignment.

## Stock workflow sets

### `inner_outer_eval`

This set works unchanged as a standalone root or as a child:

- `outer` is the durable orchestrator. It owns plan revision, task selection
  and acceptance, current state, decisions, accepted-work ledger, handoff, and
  successful completion.
- `inner` implements and verifies one leaf selected by outer. It reports
  evidence upward and never invents or rewrites a missing plan.
- `eval_reviewer` may author or revise outcome-oriented `harness_judge`
  checks.
- `eval_runner` may run those checks and publish provenance-rich observations.

The eval roles are scheduled helpers. Outer may use their output, wait for an
imminent scheduled run, coordinate another review, or decide from stronger
repo tests, delivery facts, child evidence, or direct inspection.

### `pm_planner_dispatcher`

The PM layer has only two scheduled roles:

- `planner` owns the high-level program plan, child acceptance/rerouting,
  current state, decisions, handoff, optional final evaluation, and completion.
- `dispatcher` transports one planner-selected outcome into one child request.
  It does not decompose the outcome into implementation leaves or accept the
  child's result.

Dispatch phase, milestone, or integrated-feature outcomes that leave meaningful
planning to the child outer. For example, “make the development foundations
ready and evidenced” is appropriate; “edit this exact file and run one exact
check” belongs to a child plan. A target goal may require the planner to run a
prepared final eval suite, but that target-specific requirement does not make
evaluation mandatory for every loop.

### Workflow cadence

Each role's `config.yaml` may use:

```yaml
enabled: true
run_every: 1
must_follow: outer
not_before_iteration: 1
priority: 20
run_on_start: false
run_after_successes:
  workflow_id: inner
  every: 3
emits_goal_check: false
description: "Plain-language scheduled responsibility."
```

`must_follow` and `run_after_successes.workflow_id` must name roles in the same
set. Higher priority wins among eligible roles. Cadence counts completed
iterations, not time. In protocol v3, `emits_goal_check` is not needed for
successful completion; stock evals publish advisory receipts instead.

## Assignment first: never reconstruct paths

At the beginning of every attempt, read the absolute `assignment.json` path
rendered near the top of the prompt. Confirm its root/session/depth,
workflow/role, attempt, protocol version, scoped goal hash, and ownership.
Then use its exact `absolute_paths` values. Never concatenate
`.loopy_loop/sessions/...`, infer the active child from directory names, reuse
a path from another attempt, or substitute the repository-root goal for the
current layer goal.

A protocol-v3 Assignment names at least:

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

It also has stable optional `parent_goal`, `parent_goal_contract`,
`parent_handoff`, and `accepted_child_request` keys. They are null when the
topology does not provide them. In a child, `layer_inputs` is the immutable
child-local copy—not the parent's mutable source.

Durable artifacts cite portable confined references such as `repo:/...`,
`session:/...`, `parent:/...`, `root:/...`,
`session:<session-id>:/...`, and `trace:<manifest-id>:/...`. Running agents
receive absolute paths; durable records use logical references so the checkout
can move.

## Visible layer state

Every protocol-v3 session has a compact semantic spine:

```text
project_state/
├── plan.md
├── tasks/
├── current_state.md
├── decisions/
├── finished.md
├── eval_state.md
└── handoff.json
```

- `plan.md` records revisioned outcomes, dependencies, one active selection,
  and replanning triggers.
- `tasks/` gives stable leaf or milestone records with criteria, status, and
  evidence.
- `current_state.md` is the short cold-start view of active work, blockers,
  risks, and next decision.
- `decisions/` preserves consequential choices and rationale.
- `finished.md` is append-only accepted work, written by the layer
  orchestrator after review.
- `eval_state.md` indexes optional eval intent, observations, disagreements,
  and possible next actions.
- `handoff.json` is the orchestrator's rolling semantic summary for a parent or
  root operator.

Outer/planner updates this state after material decisions and monotonically
increments the handoff revision. Inner and dispatcher contribute factual
evidence but do not race the orchestrator-owned plan, status, acceptance, or
handoff fields. The engine enforces identity, topology, schemas, hashes, and
reference containment; it does not parse prose to decide whether a plan or
completion judgment is wise.

Two additional artifacts make scheduling inspectable:

- `workflow_roster.json` is frozen for the session. It lists all scheduled
  roles, responsibilities, cadence, expected outputs, and authorities.
- `scheduler_view.json` is frozen for the attempt. It states which role would
  run next if this attempt returns normally and no control, child request,
  stop, failure, input, or recovery changes state. It is a conditional
  forecast, not a reservation or gate.

Read both before duplicating work that a scheduled role is already likely to
perform.

The Assignment's `user_inputs` path names the append-only
`inputs/user_updates.jsonl` journal. Preserve its history; consume and
acknowledge inputs through the documented journal protocol instead of editing
old records.

## Dynamic Team Harness delegation

The selected workflow is a durable role; agents spawned through Team Harness
are temporary delegates inside that attempt. A nested `type=harness` spawn is
another temporary coordinator, not another loop layer. Only an accepted Loopy
child request creates a durable child session.

The workflow coordinator decides dynamically whether to delegate, what roles
are useful, how many agents to use, and which independent tasks can run in
parallel. Give every direct spawn a focused task, expected outputs, state
responsibility, and relevant absolute Assignment paths. Delegates report back;
the workflow coordinator owns integration and the durable role output.

For consequential independent analysis, prefer parallel delegates from
different enabled harness families when useful. Once an artifact or diff is
stable, prefer review by another enabled family when that materially improves
confidence. Eval-check creation deserves the strongest version: independent
cross-family coverage analysis, one integrator, then different-family review
for blind spots, gameability, false positives/negatives, and implementation
coupling. These are judgment defaults—not quotas, fixed graphs, or gates.

Team Harness writes a separate absolute `agent_assignment.json` and output
area for each direct spawn. Scratch work, candidate analyses, transcripts, and
verbose output stay in traces; conclusions needed by later attempts must be
promoted into compact layer state.

## Advisory evaluation

Evaluation belongs to the current layer's scoped goal. A child eval cannot
complete its parent. Stock eval definitions are outcome-oriented
`harness_judge` checks; agents do not invent deterministic checks. Repo-owned
tests and prepared eval suites remain valid independent evidence.

An eval runner uses the Assignment's absolute `repo_root`, `eval_checks`,
`eval_receipts`, `raw_eval_output`, and capability-roster paths. It keeps raw
reports in the attempt trace, writes compact canonical receipts in session
state, and records actual judge family/model/effort provenance. Missing,
malformed, non-passing, or stale eval output is a diagnostic for the
orchestrator—not a failed harness iteration, completion veto, or reason to
starve outer/planner.

If an orchestrator cites an eval receipt in terminal control, the reference
and provenance must validate. It may also complete without citing or running an
eval. Strict provenance answers “what produced this observation?”; the
orchestrator decides how much semantic weight it deserves.

## Protocol-v3 terminal control and outcome

Only the workflow contract's `orchestration.completion_role` may publish
successful control. In the stock sets that is `outer` or `planner`:

```json
{
  "schema_version": 3,
  "control_id": "stable-unique-id",
  "state": "stopped",
  "stop_reason": "goal_met",
  "reason": "Why this layer's own goal is complete.",
  "producer": {
    "session_id": "current-session-id",
    "workflow_id": "outer-or-planner",
    "attempt_id": "current-attempt-id"
  },
  "evidence_refs": [],
  "eval_receipt_refs": [],
  "handoff_ref": "session:/project_state/handoff.json",
  "created_at": "RFC3339 timestamp"
}
```

Eval references and `handoff_ref` are optional, but every cited reference must
be truthful. Publish protocol files with a complete same-directory temporary
file followed by atomic rename.

The only human escape hatch is `stop_reason: unresolvable_error`. Use it only
for a genuinely terminal human-only decision, missing credential, or
unauthorized destructive/billable action after autonomous repair, retry,
re-scoping, and alternate routes are exhausted. Use the exact current producer
identity, a specific reason, non-empty `attempted_routes`, and truthful
`evidence_refs`. There is no paused or waiting-for-human state.

At a terminal transition, the engine writes topology-neutral
`session_outcome.json`, binding the authenticated control, observed handoff
revision/hash, and available evidence. Root operators consume that file
directly. A parent's `child_outcomes/` record links the same child outcome; it
does not synthesize a second story. Missing or malformed handoff is diagnosed
in the outcome rather than overriding authentic orchestrator control.

## Recursive child sessions

The PM dispatcher publishes one unique typed request atomically below the
Assignment's absolute `child_requests` path. Freeze and hash immutable input
snapshots before publishing the request. Keep the selected goal at milestone
granularity so the child outer can build its own plan.

The coordinator validates and archives the request, copies verified inputs to
the child's `layer_inputs`, suspends the parent, and runs only the deepest live
session. The same edge can recurse to any depth. When the child terminates, the
engine writes the linked child outcome and resumes the parent. Planner then
records `accepted`, `rework`, or `reroute`; child success never closes an
ancestor automatically.

Request ID is the idempotency key. Never duplicate a pending/live request, use
a mutable plan as immutable child input, or write a temporary child-request
filename ending in `.json`.

## Durable state versus traces

Keep the two retention planes separate:

- `.loopy_loop/sessions/` contains compact durable truth needed for recovery,
  planning, scheduling, handoff, evaluation provenance, and outcomes.
- `.loopy_loop/traces/` contains detailed observable I/O: rendered prompts,
  Team Harness turns, direct-spawn assignments and outputs, stdout/stderr, raw
  eval reports, verbose git evidence, timing, provider/process identity, and
  usage.

Both are gitignored. Scheduling and semantic continuity must not depend on
retaining raw trace bytes or rereading huge reports. Preserve raw evidence in
traces; promote only compact conclusions and references into session state.

## Run, monitor, and recover

Start two processes in separate terminals:

```bash
# coordinator
loopy coordinator --host 127.0.0.1 --port 8080

# the one worker
loopy worker --coordinator http://127.0.0.1:8080
```

Export the configured API-key environment variable in both shells when the
provider requires it. The worker registers its process and repository
identity plus protocol-v3 capabilities. The coordinator rejects an older or
incomplete worker before dispatch.

Useful operations:

```bash
loopy status
loopy status --json
loopy status --watch
loopy events --follow
loopy update TEXT...
loopy stop
loopy stop --force
loopy reload
loopy traces list
loopy traces inspect MANIFEST_OR_ID
loopy coordinator --resume
```

`status` and `events` walk the active session stack. Status includes the active
attempt's last raw/output write and any unexpired model-family rate-limit
circuits; `--json` exposes the same observations as structured fields.
`loopy stop` is the tree-wide stop request and applies at a safe
register/finish boundary. `loopy stop --force` also invokes the existing
same-host process-group reaper for the active iteration. `loopy reload`
refreshes workflow prompts and coordinator-operational recovery/failure/cost
settings at the next task boundary; it never changes a session-frozen config
snapshot, workflow roster/contract, or capability roster.
`--resume` reconstructs the active
path and continues the deepest live session. Exactly one worker is deliberate;
a second verifiably live worker is refused.

A successful `IterationResult` means only that Team Harness returned without
an execution error. The orchestrator still reviews semantic quality. Crash
recovery may salvage a produced result, drain/reap orphaned harness processes
according to policy, or record an abandoned mechanical attempt before normal
scheduling resumes.

## Common mistakes

- Reading the repo-root goal instead of the Assignment's `layer_goal_contract`.
- Constructing session paths from cwd instead of using absolute Assignment
  values.
- Treating `scheduler_view` as a promise rather than a conditional forecast.
- Letting inner bootstrap the plan or dispatcher pre-plan child leaves.
- Giving eval-runner successful-control authority or requiring an eval to
  finish.
- Treating a child outcome as automatic parent acceptance.
- Treating a direct Team Harness spawn as a durable loop role or child.
- Hard-coding model names instead of selecting available roster bundles.
- Using a family count, review quota, or eval result as an engine gate.
- Leaving future-needed conclusions only in raw traces.
- Pausing for a human instead of solving autonomously or using the terminal
  last-resort blocker contract.

## Reference

- HTTP contract: `docs/http-contract.md`
- Session layout: `docs/session-layout.md`
- Architecture decisions: `design/decisions.md`
- Binding v3 design:
  `design/designs/orchestrator-owned-completion-and-cross-harness-review.md`
- Source: https://github.com/writeitai/loopy-loop
