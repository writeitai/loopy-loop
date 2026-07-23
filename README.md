# loopy-loop

<p align="center">
  <img src="docs/images/logo.png" alt="loopy-loop logo" width="400">
</p>

`loopy-loop` runs long-running AI agent workflows inside your repository.
It turns a goal file in your repository into an inspectable sequence of agent
iterations: plan, implement, review, optionally evaluate, record evidence, and
continue until the goal is met or the loop hits a terminal blocker.

The value is control and durability. Instead of asking one agent to solve a
large task in one fragile chat, loopy-loop gives each durable goal layer a
persistent session directory, immutable attempt assignments, explicit stop
conditions, and structured evidence. You can stop and resume the service,
append instructions while it runs, audit what happened, inspect attempt traces,
and keep the actual project changes in normal git branches and PRs.

Under the hood, loopy-loop runs a small FastAPI coordinator and a single
worker. The coordinator owns the loop state and chooses the next workflow. The
worker runs assignments through
[`team-harness`](https://github.com/writeitai/team-harness), which can delegate
to agent CLIs such as Codex, Claude Code, and Gemini. The packaged
`inner_outer_eval` template also uses
[`eval-banana`](https://github.com/writeitai/eval-banana) conventions for
session-scoped evaluation checks; eval-banana installs automatically as a
loopy-loop dependency.

## Install

Install the CLI from the official [PyPI package](https://pypi.org/project/loopy-loop/).

With `uv`, install it as a command-line tool:

```bash
uv tool install loopy-loop
```

Or with `pip`:

```bash
pip install loopy-loop
```

For development inside this repository:

```bash
uv sync --extra dev
```

The stock protocol-v3 contract spans three owned projects. It requires
`team-harness>=0.5.4` for caller-owned run records, nested assignment context,
capability-roster propagation, and canonical agent streams. When a workflow
uses evaluation, `eval-banana>=0.5.0` supplies hermetic
`--no-project-config` runs, explicit harness selection, canonical check
digests, retained judge evidence, and the `--result-out` provenance-stamped
result the stock `inner_outer_eval` eval runner writes to `eval_results.md`
(D14). Install compatible releases of all three projects together.
For coordinated development across the repositories, install the corresponding
team-harness and eval-banana checkouts as editable dependencies:

```bash
uv pip install -e /path/to/team-harness -e /path/to/eval-banana
```

An older dependency is not a reduced-fidelity mode: a fresh stock session
fails registration clearly if its worker or harness cannot advertise the
required protocol-v3 capabilities.

## Install the Agent Skill

This repo also ships an [Agent Skill](https://support.claude.com/en/articles/12512176-what-are-skills)
that teaches Claude Code, Codex, and compatible agents how to set up and run
loopy-loop in another target repo.

```bash
npx skills add https://github.com/writeitai/loopy-loop --skill loopy-loop
```

The skill source lives under [`skills/loopy-loop/`](./skills/loopy-loop/SKILL.md).

## Initialize a Target Repo

Run this from the repository you want agents to work on:

```bash
loopy init --template inner_outer_eval
```

This is the recommended starting template. It creates:

- `loopy_loop_config.yaml`
- `loopy_loop_goal.txt`
- `.loopy_loop/workflow_sets/inner_outer_eval/contract.yaml`
- `.loopy_loop/workflow_sets/inner_outer_eval/workflows/outer/`
- `.loopy_loop/workflow_sets/inner_outer_eval/workflows/inner/`
- `.loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_reviewer/`
- `.loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_runner/`
- additive `.gitignore` entries for session state, traces, the trace-finalization
  outbox, repository identity, and root state/lock/archive files

For a double loop, initialize the program-level template instead:

```bash
loopy init --template pm_planner_dispatcher
```

Its planner works in high-level phase or milestone outcomes. Its dispatcher
turns one selected outcome into an `inner_outer_eval` child session, whose
outer role owns the detailed leaf plan. The program layer deliberately has no
scheduled eval roles; the planner may coordinate optional program-level or
goal-required final evaluation itself.

`loopy init` is idempotent. It creates missing files and leaves existing files
alone — except `.gitignore`, which is updated additively with all runtime ignore
rules.

## Write the Goal

The loop goal lives in `loopy_loop_goal.txt`. Replace the scaffolded example
with the real target, including constraints and observable completion criteria.

Example:

```text
Implement passwordless email login.

Completion criteria:
- Users can request a one-time login link from the sign-in page.
- The link expires after 15 minutes and cannot be reused.
- Existing password login keeps working.
- Tests cover token expiry, token reuse, and successful login.
- README documents required environment variables.
```

Keep the goal specific enough that its orchestrator and reviewers can assess
whether the scoped outcome is complete. For one-off overrides, start the
coordinator with `--goal-file PATH`; the file is copied into the session as
`goal.md`.

## Run the Loop

Start the coordinator in one terminal:

```bash
loopy coordinator --host 127.0.0.1 --port 8080
```

Start a worker in another terminal:

```bash
loopy worker --coordinator http://127.0.0.1:8080
```

Useful control commands:

```bash
loopy status
loopy status --json
loopy update Prioritize the failing integration test
loopy stop
loopy stop --force
loopy reload
loopy traces list
```

If the coordinator stops while a session is still running, restart it with:

```bash
loopy coordinator --host 127.0.0.1 --port 8080 --resume
```

The default templates use `team_harness_provider: "codex"`, so the coordinator
uses local Codex authentication. If you switch to an OpenAI-compatible provider,
export the environment variable named in `team_harness_api_key_env`, usually
`OPENROUTER_API_KEY`, in both the coordinator and worker shells.

## How It Works

At a high level:

1. `loopy init` writes a root config, a goal file, and workflow files into the
   target repo.
2. `loopy coordinator` loads `loopy_loop_config.yaml`, freezes the goal and
   stock protocol-v3 workflow contract, creates a root session under
   `.loopy_loop/sessions/`, and exposes `/register` and `/finished`.
3. A protocol-v3 worker advertises its Loopy and team-harness capabilities plus
   repository identity. The coordinator dispatches only to a matching checkout
   with the required capability-roster context support.
4. For each attempt, the coordinator freezes the exact workflow
   config/prompt/contract, an assignment, and a conditional scheduler view.
   The assignment gives the harness coordinator absolute paths to its scoped
   goal, plan, tasks, decisions, handoff, workflow roster, scheduler view,
   capability roster, outputs, and trace directory. Durable receipts still use
   portable logical references.
5. Before calling a model, the worker verifies that assignment and its trace,
   records the exact task response, and writes rendered prompt and git-before
   evidence.
6. `team-harness` runs the coordinator model. It may dynamically spawn Codex,
   Claude Code, Gemini, or another enabled family. Each direct spawn receives
   the current layer identity, relevant absolute state paths, the frozen
   capability roster, and its focused delegated assignment. The workflow
   coordinator remains accountable for integration.
7. The worker posts a completion bound to the exact worker, repository,
   attempt, and assignment hash. A successful iteration means the harness ran
   without an execution error; it does not mean the work was semantically
   accepted. The coordinator records and seals the observable execution under
   `.loopy_loop/traces/`.
8. The coordinator enforces protocol facts such as identity, topology,
   schemas, hashes, and reference containment. The workflow contract's durable
   orchestrator decides semantic acceptance and completion from the available
   evidence. A terminal protocol-v3 session receives a topology-neutral
   `session_outcome.json` that links its control, handoff, and available
   evidence.

The `inner_outer_eval` template is organized around four workflows:

- `outer`: owns the layer plan, selects and accepts leaves, maintains compact
  resumption state and the upward handoff, and alone decides successful
  completion for this layer.
- `inner`: implements and verifies the one leaf selected by `outer`, then
  reports evidence upward. It does not create the layer plan or accept its own
  work.
- `eval_reviewer`: may create or refresh outcome-oriented, session-scoped
  `harness_judge` checks as advisory evidence.
- `eval_runner`: may run those checks and publish provenance-rich receipts for
  `outer` to weigh. It does not write `goal_check.json` or successful terminal
  control.

The `pm_planner_dispatcher` template has two durable roles:

- `planner`: owns a high-level phase/milestone plan, accepts or reroutes child
  outcomes, maintains the program handoff, and decides program completion.
- `dispatcher`: faithfully turns the one selected milestone outcome into a
  child request. It does not pre-plan the child's leaves or accept the child's
  result.

Evaluation is optional evidence in both sets. The completion owner can run or
delegate checks, wait for a scheduled eval role, or decide from stronger
repository, review, child, test, git, or delivery evidence. Protocol-v3
`control.json` binds `goal_met` to the exact current orchestrator attempt;
`evidence_refs`, plural `eval_receipt_refs`, and `handoff_ref` are optional,
but any cited reference must validate. `unresolvable_error` remains the
last-resort stop for a genuinely terminal blocker after autonomous routes are
exhausted.

The loop does not hide state inside a chat transcript. Each layer has a visible
semantic spine in `project_state/`—plan, stable task records, current state,
decisions, accepted-work ledger, optional eval index, and rolling handoff. A
session-frozen workflow roster explains every scheduled role; an
attempt-frozen scheduler view forecasts the next role under explicit
assumptions; and the root capability roster shows every enabled harness family
across the configured strength tiers. Detailed prompts, model turns, tool and
spawn I/O, raw eval reports, and verbose logs live separately in the gitignored
trace tree.

## Repo Layout

After initialization, the target repo has this shape:

```text
target repo/
├── loopy_loop_config.yaml
├── loopy_loop_goal.txt
└── .loopy_loop/
    ├── repository.json                 # ignored checkout identity
    ├── workflow_sets/
    │   └── <workflow_set>/
    │       ├── contract.yaml
    │       └── workflows/<workflow_id>/
    │           ├── config.yaml
    │           └── prompt.txt
    ├── sessions/
    │   └── <session_id>/
    │       ├── goal.md
    │       ├── goal_contract.json
    │       ├── session.json
    │       ├── workflow_contract.json
    │       ├── workflow_roster.json
    │       ├── harness_capability_roster.json  # root, shared by session tree
    │       ├── state.json
    │       ├── control.json                    # written only to stop
    │       ├── session_outcome.json            # terminal projection
    │       ├── inputs/{user_updates.jsonl,accepted_request.json,artifacts/}
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
    │       ├── child_requests/{pending,accepted,rejected}/
    │       ├── child_outcomes/
    │       ├── parent_acceptance/
    │       ├── git_receipts/
    │       ├── delivery_receipts/
    │       ├── trace_seals/
    │       ├── iterations/<n>/<role>/workflow_snapshot/<attempt>/
    │       │   ├── assignment.json
    │       │   └── scheduler_view.json
    │       └── children/<child_session_id>/...
    ├── traces/<root>/sessions/<session>/attempts/<attempt>/
    └── trace_finalization_outbox/
```

Workflow definitions are part of the repo and should usually be committed.
Session directories, traces, the trace-finalization outbox, and
`repository.json` are runtime output and are ignored by default. Session
state/evidence is required for recovery; trace retention is independent from
that compact truth.

## Configuration

Root config lives at `loopy_loop_config.yaml`:

```yaml
goal_file: loopy_loop_goal.txt
workflow_set: inner_outer_eval
max_turns: 160
goal_check_consecutive_failures_cap: 3
# workflow_consecutive_failures_cap: 5
team_harness_provider: "codex"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
  - "claude"
  - "gemini"
team_harness_agent_models:
  codex: "gpt-5.5"
  claude: "claude-opus-4-8"
  gemini: "gemini-3.5-flash"
team_harness_agent_reasoning_efforts:
  codex: "high"
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
```

Important rules:

- `workflow_set` selects the default workflow set for new sessions.
- `goal_file` is resolved relative to `loopy_loop_config.yaml`.
- Inline `goal` values in YAML are rejected; the goal should live in a file.
- `max_turns` is the maximum number of completed workflow iterations.
- `team_harness_model` controls the team-harness coordinator model.
- `team_harness_agent_models` controls default models for worker subprocesses.
- `model_tiers` (optional) maps semantic strength → enabled harness family →
  `{model, effort}`. The four stock tiers are `frontier` for maximum-capability
  and highest-stakes work, `strong` for complex work, `standard` for the
  balanced default, and `economy` for bounded low-risk work. At root-session
  creation, Loopy freezes every family/tier cell—including unavailable cells—
  into `harness_capability_roster.json`; the assignment and team-harness caller
  context carry that roster to coordinators and nested delegates. Agents choose
  proportionately and are encouraged to use another enabled family for useful
  independent review, especially during eval-check design. This remains
  guidance, not a quota or semantic gate (D8/D9).

  With `default_tier` set, that tier derives
  `team_harness_agent_models` and
  `team_harness_agent_reasoning_efforts`; it must cover every configured
  family, and the two flat mappings must be omitted. Without `default_tier`,
  flat per-family model settings appear as configured `standard` bundles.

  ```yaml
  model_tiers:
    frontier:
      codex: {model: "<frontier codex model>", effort: "<effort>"}
      claude: {model: "<frontier claude model>", effort: "<effort>"}
      gemini: {model: "<frontier gemini model>", effort: "<effort>"}
    strong:
      codex: {model: "<strong codex model>", effort: "<effort>"}
      claude: {model: "<strong claude model>", effort: "<effort>"}
      gemini: {model: "<strong gemini model>", effort: "<effort>"}
    standard:
      codex: {model: "<standard codex model>", effort: "<effort>"}
      claude: {model: "<standard claude model>", effort: "<effort>"}
      gemini: {model: "<standard gemini model>", effort: "<effort>"}
    economy:
      codex: {model: "<economy codex model>", effort: "<effort>"}
      claude: {model: "<economy claude model>", effort: "<effort>"}
      gemini: {model: "<economy gemini model>", effort: "<effort>"}
  default_tier: "standard"
  ```
- `team_harness_api_base` is normalized by loopy-loop: trailing slash stripped,
  `/v1` appended when missing.
- `team_harness_max_retries`, `team_harness_retry_base_delay_s`, and
  `team_harness_retry_max_delay_s` are optional retry controls for transient
  team-harness API/network errors.
- `recovery_policy` (`drain` by default, or `reap`) and
  `recovery_drain_timeout_s` control what crash recovery does with agent
  processes left by an interrupted worker task: drain lets them finish within
  one shared bounded deadline; reap kills them immediately. The interrupted
  task is recorded as abandoned, consumes a turn, and then normal scheduling
  continues only if no stop condition fires. These are coordinator-side
  settings and are not part of the config snapshot sent to the worker.
- `workflow_consecutive_failures_cap` (default 5) is a per-workflow circuit
  breaker: that many consecutive failed iterations of the same workflow stop
  the loop with `stop_reason="workflow_failure_cap"` instead of retrying a
  wedged workflow until `max_turns`. Any success of the workflow resets its
  counter. Coordinator-side only; not part of the config snapshot sent to
  the worker.
- `model_prices` (optional, coordinator-side only) sets USD prices per 1M
  tokens for the harness coordinator model (`prompt_usd_per_1m`,
  `completion_usd_per_1m`); with prices set, `loopy status` derives an
  estimated cost from the token ledger. `max_cost_usd` (optional, requires
  `model_prices`) stops the loop with `stop_reason="max_cost_usd"` once the
  session tree's estimated cost reaches the budget. Cost covers the harness
  COORDINATOR model only — agent-CLI subprocesses (codex, claude, gemini)
  bill through their own accounts and are not measurable here.

An explicit `loopy reload` refreshes the running coordinator at the next task
boundary. It reloads workflow `prompt.txt` contents and these non-frozen,
coordinator-operational root settings: `recovery_policy`,
`recovery_drain_timeout_s`, `workflow_consecutive_failures_cap`,
`max_cost_usd`, and `model_prices`. The update is atomic across the
coordinator's cached workflow sets.

Reload deliberately does **not** change a live session's frozen goal or
`config_snapshot` (including harness provider/model/families, retry settings,
system prompt extension, criteria, and turn limit), its workflow membership or
`config.yaml` cadence, its workflow contract/roster, or its capability roster.
Those changes require a coordinator restart and a new session. A later child
workflow set that has never been loaded still freezes its on-disk definition
when that child session is created.

Workflow config lives beside each workflow prompt:

```yaml
enabled: true
priority: 0
run_every: 1
must_follow: null
not_before_iteration: 0
run_on_start: false
run_after_successes: null
description: ""
```

Workflow rules:

- The workflow id is the folder name under
  `.loopy_loop/workflow_sets/<workflow_set>/workflows/`.
- `priority` breaks ties among eligible workflows; higher values run first.
- `run_every` is based on completed iteration count, not wall clock.
- `run_on_start=true` makes a workflow eligible before any successful workflow
  has run.
- `must_follow` and `run_after_successes.workflow_id` must reference existing
  workflow ids.
- `run_after_successes` can schedule a workflow after every N successful runs
  of another workflow:

```yaml
run_after_successes:
  workflow_id: inner
  every: 10
```

Each workflow set declares `contract.yaml`. It names the layer kind, every
scheduled role's responsibility and accountable state paths, the one
orchestrator that owns planning/handoff/completion, optional advisory eval
authors and runners, terminal-blocker reporting roles, and whether the set uses
the recursive child interface. This is accountability metadata and prompt
context, not a filesystem ACL or semantic scheduler gate (D8). The stock sets
declare `session_protocol_version: 3`; the coordinator validates that the same
orchestration role owns plan, handoff, and successful completion. Historical
custom v1/v2 contracts remain resumable under the version frozen in their
session.

## Output and Logging

Each coordinator run creates a root under `.loopy_loop/sessions/`. Recursive
workflow sets may create child sessions beneath it, but one worker still
advances only the deepest active session. Session files hold compact durable
truth: scoped goals, the semantic state spine, workflow and capability rosters,
attempt scheduler views, task/recovery state, optional eval receipts, child
handoffs, control, terminal outcomes, and git/delivery evidence.

Each attempt freezes its workflow sources and `assignment.json`. The
assignment identifies the exact repository/session/workflow/attempt and gives
the harness coordinator absolute paths to its own layer state and outputs,
along with compact roster and scheduler context. Durable receipts use logical
`session:/`, `parent:/`, `root:/`, and `trace:` references so a stopped checkout
can move. Team-harness gives every direct spawn its own absolute
assignment/output paths, current layer identity, dynamic delegated task, and
inherited capability roster; the original coordinator remains accountable for
integrating the result.

Detailed observable execution goes to the separately gitignored
`.loopy_loop/traces/<root>/sessions/<session>/attempts/<attempt>/` tree. It
contains the attempt manifest, protocol exchange, canonical team-harness run,
direct-agent streams, raw eval output, and verbose git/service records. These
are raw local records and may contain private data. A sealed manifest means
the local inventory is integrity-checked and its channel completeness is
known; it does not mean the attempt succeeded semantically.

Use `loopy status` and `loopy events` for compact progress, and `loopy traces
list` plus `loopy traces inspect` for attempt detail. The complete artifact and
writer/reader reference is [docs/session-layout.md](docs/session-layout.md).
Fresh stock workflow sets use protocol v3.

## Control and Completion

Each session evaluates its own scoped goal. A delivery child may prove its task
while its parent still needs integration or release work. In the packaged
`inner_outer_eval` set, `outer` accepts task evidence and owns completion. In
`pm_planner_dispatcher`, `planner` independently accepts child outcomes and
owns program completion. Eval roles and dynamically spawned delegates report
evidence to those orchestrators; they cannot complete another role's layer.

Successful protocol-v3 control is identity-bound to the exact current attempt
and must come from the completion role frozen in the workflow contract:

```json
{
  "schema_version": 3,
  "control_id": "control-goal-met-id",
  "state": "stopped",
  "reason": "Why this scoped goal is complete",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "session-id",
    "workflow_id": "outer",
    "attempt_id": "attempt-id"
  },
  "evidence_refs": [],
  "eval_receipt_refs": [],
  "handoff_ref": "session:/project_state/handoff.json",
  "created_at": "2026-07-17T12:00:00Z"
}
```

The three evidence fields are optional. An orchestrator does not need an eval
to complete the goal. A missing, malformed, stale, non-passing, or conflicting
advisory eval from an otherwise completed harness attempt is recorded as a
diagnostic; it does not consume workflow-failure budget or become an engine
veto. If control cites an artifact, however, its reference, hash, subject, and
accepted provenance must be truthful.

The one D5 escape hatch is an identity-bound v3 `unresolvable_error`. It is for
a genuinely terminal blocker after autonomous alternatives are exhausted:

```json
{
  "schema_version": 3,
  "control_id": "control-blocker-id",
  "state": "stopped",
  "reason": "specific terminal blocker",
  "stop_reason": "unresolvable_error",
  "producer": {
    "session_id": "session-id",
    "workflow_id": "inner",
    "attempt_id": "attempt-id"
  },
  "attempted_routes": ["retry", "re-scope", "alternate local route"],
  "evidence_refs": ["session:/protocol_failures/blocker.json"],
  "created_at": "2026-07-15T12:00:00Z"
}
```

Both successful and blocker control must identify the exact current
session/workflow/attempt. A blocker producer must be authorized by the frozen
contract and list the autonomous routes already tried. Invalid control is
archived with repair diagnostics instead of being treated as semantic truth.
Repeated broken protocol or workflow execution is bounded by the configured
failure caps.

The exact eval-receipt acceptance, successful control, blocker, and rejection
rules are in [docs/http-contract.md](docs/http-contract.md).

## Workflow Sets and Child Sessions

Workflow sets are mandatory. Even a single-loop repo uses:

```text
.loopy_loop/workflow_sets/main/workflows/...
```

The older `.loopy_loop/workflows/...` layout is not loaded.

A session workflow may request one sequential child by atomically publishing a
unique `*.json` file under the assignment's absolute
`child_requests/pending/` path. The same edge works recursively, so one-loop,
planner/dispatcher, and deeper trees use one state machine. Only the deepest
session runs an assignment; every ancestor is suspended on one child.

The child-request payload currently uses schema version 2. It carries parent
provenance, a child-scoped outcome and completion criteria, and hashed input
references. The coordinator archives the accepted request and copies each
verified input into the child's immutable `inputs/` area. Child attempts
receive those logical references, hashes, and absolute local paths; later
parent edits cannot change accepted work.

The stock PM dispatcher first freezes the selected milestone and its planning
evidence under `project_state/dispatch_inputs/<request_id>.json`, then hashes
that immutable snapshot into the request. Only after the request is atomically
published does it add the factual request/lifecycle link to the stable task
record. Mutable plan and task prose are never used as hashed child inputs.

When the child stops, the engine writes its topology-neutral
`session_outcome.json` and links that result from the parent's factual child
outcome. The parent then writes a separate acceptance, rework, or reroute
decision after reviewing the child's handoff and evidence. Terminal
descendants unwind iteratively, so the same edge supports three or more active
depths without a depth-specific scheduler. Invalid requests are archived with
reasons and can be repaired autonomously.

The packaged `pm_planner_dispatcher` workflow set uses this contract for PM
orchestration:

- `planner` maintains the high-level phase/milestone plan, selects one outcome,
  reviews terminal child evidence, owns parent acceptance and the rolling
  handoff, and decides completion.
- `dispatcher` freezes the selected outcome into an immutable request input,
  publishes the schema-v2 child request, and tracks factual lifecycle evidence
  without deciding acceptance.

The planner can run prepared final evaluations named by the target goal or
delegate other program-level review when useful. These are planner inputs, not
additional scheduled PM roles.

## HTTP Contract

The coordinator exposes exactly two endpoints:

- `POST /register`
- `POST /finished`

Both return a `TaskResponse` with `action` equal to `"run"` or `"stop"`.

Fresh stock registration requires worker protocol 3 and the declared Loopy and
team-harness capabilities, then binds the worker to the absolute checkout and
stable repository ID. A missing capability returns HTTP 426 without advancing
state. A run response identifies the frozen session/workflow/attempt assignment
and its absolute protocol-v3 context; completion must echo its worker,
repository, attempt, and assignment hash. Stale or mismatched completion cannot
mutate current work. Durable local result and pending-completion records allow
the next registration to recover an interrupted acknowledgement exactly once.

The authoritative payload models are `RegisterRequest`, `TaskResponse`, and
`FinishedRequest` in
[`src/loopy_loop/models.py`](src/loopy_loop/models.py). The recursive ownership
and compatibility rules are in [docs/http-contract.md](docs/http-contract.md).

## CLI Reference

```bash
loopy init [--template default|inner_outer_eval|pm_planner_dispatcher]
```

Scaffolds loopy-loop files. The compatibility `default` template creates only
the historical `goal_check` workflow. `inner_outer_eval` creates the
recommended protocol-v3 outer/inner/advisory-eval workflow set.
`pm_planner_dispatcher` creates the two-role planner/dispatcher program layer
and also ships the `inner_outer_eval` child set its dispatcher spawns, so a
clean init is executable end to end.

```bash
loopy coordinator --host 0.0.0.0 --port 8080 [--resume] [--workflow-set NAME] [--goal-file PATH]
```

Runs the coordinator. `--workflow-set` and `--goal-file` override the root
config for the new session. `--resume` reuses a non-terminal latest session.

```bash
loopy worker --coordinator http://127.0.0.1:8080
```

Runs a blocking worker until the coordinator returns `action: "stop"`.

```bash
loopy status           # stack, liveness, family health, usage, estimated cost
loopy status --json    # the same status as machine-readable JSON
loopy status --watch   # re-render every 2 seconds
loopy events           # the active session's event stream
loopy events --follow  # tail it live (--json for raw lines)
loopy update TEXT...   # append to the deepest active layer
loopy update --session SESSION_ID TEXT...
loopy stop             # tree-wide stop at the next safe boundary
loopy stop --force     # also reap the active iteration's tracked agent CLIs
loopy reload           # refresh prompts/operational config at the next boundary
loopy traces list
loopy traces inspect MANIFEST_OR_ID
```

`status` prints the latest session state — the whole session stack while a
child runs (the live child is shown under its suspended parent), each
session's subtree token usage, and (with `model_prices` configured) estimated
cost. For an active task it also reports the newest mtime anywhere in that
attempt's raw/output tree as `last activity: Ns ago`, plus unexpired
`rate_limited_families` found in the current team-harness `run.json` as
`model families rate-limited: FAMILY until TIME`. Missing, incomplete, or
older run records are tolerated. `status --json` exposes per-session
`last_activity_at`, `last_activity_age_s`, `rate_limit_data_available`, and
`rate_limited_families` fields.

`update` appends the input record exactly as supplied; without `--session` it
is routed to the deepest active layer and later assignments append delivery
and acknowledgement records rather than editing history. `stop` projects root
stop intent through the whole active path and takes effect at the next
register or finish boundary. `stop --force` first records that same durable
tree-wide intent, then invokes the existing recovery reaper with immediate
`reap` policy and reports harness-run, settled-agent, and unsettled-agent
counts. Force reaping is limited to tracked process groups on the local host;
a remote active worker is reported as unreachable rather than pretending its
agents were stopped.

`reload` writes an ignored, atomic reload generation under the sessions root.
At the next task boundary, the coordinator reruns preflight and atomically
refreshes only the prompt/operational fields described in Configuration above.
It does not mutate an already-frozen attempt; the following attempt receives
the refreshed prompt in its own immutable workflow snapshot.

Trace commands accept a manifest ID, a trace root, or a manifest path confined
to this repository's `.loopy_loop/traces/`. `inspect` prints the manifest plus
its currently observed integrity. Trace finalization is crash-safe: startup
retries only transitions proved by durable attempt history, records an
unobserved HTTP response as unavailable, and never lets trace-storage failure
change semantic acceptance. See
[docs/session-layout.md](docs/session-layout.md#caller-owned-attempt-traces) for
the exact outbox and seal ordering.

## Related Projects

- [`team-harness`](https://github.com/writeitai/team-harness): the model and
  agent-CLI orchestration layer used by the loopy-loop worker.
- [`eval-banana`](https://github.com/writeitai/eval-banana): a lightweight YAML
  evaluation framework used by the packaged eval workflows. Installed
  automatically as a loopy-loop dependency.
