# loopy-loop

<p align="center">
  <img src="docs/images/logo.png" alt="loopy-loop logo" width="400">
</p>

`loopy-loop` runs long-running AI agent workflows inside your repository.
It turns a goal file in your repository into an inspectable sequence of agent
iterations: plan, implement, evaluate, record evidence, and continue until the
goal is met or the loop hits a terminal blocker.

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

Version 0.7's recursive contract spans three owned projects. It requires
`team-harness>=0.5.0` for caller-owned run records, pre-call coordinator input,
spawn assignment envelopes, and canonical stdout/stderr capture; it requires
`eval-banana>=0.3.2` for hermetic `--no-project-config` evaluation and explicit
harness-agent validation. Install all three companion changes together. While
developing before those releases are published, install the corresponding
team-harness and eval-banana checkouts into this environment as editable
dependencies:

```bash
uv pip install -e /path/to/team-harness -e /path/to/eval-banana
```

An older dependency is not a reduced-fidelity v2 mode: a fresh v2 session
fails registration clearly if the worker cannot advertise the required
capabilities.

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
- additive `.gitignore` entries for session state, traces, the export outbox,
  repository identity, and root state/lock/archive files

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

Keep the goal specific enough that a reviewer or eval workflow can decide
whether the loop is done. For one-off overrides, start the coordinator with
`--goal-file PATH`; the file is copied into the session as `goal.md`.

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
loopy update Prioritize the failing integration test
loopy stop
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
   workflow-set contract, creates a v2 root session under
   `.loopy_loop/sessions/`, and exposes `/register` and `/finished`.
3. A v2 worker advertises its protocol/capabilities and repository identity.
   The coordinator assigns work only to a matching checkout, freezes the exact
   workflow config/prompt/contract and assignment, creates the active attempt
   trace, and returns one identity-bound attempt with its assignment hash.
4. Before calling a model, the worker reopens and verifies that same trace,
   records the exact task response, verifies the frozen `assignment.json`, and
   writes the rendered prompt and git-before evidence. The assignment gives the
   harness coordinator absolute paths for its own session layer while durable
   receipts continue to use portable logical references.
5. `team-harness` runs the coordinator model. It may dynamically spawn Codex,
   Claude Code, Gemini, or other configured agents; each direct spawn receives
   an automatic assignment envelope identifying its parent attempt, delegated
   task, relevant state paths, and output directory.
6. The worker records the normalized result and compact evidence with the
   session and posts a completion bound to the exact worker, repository,
   attempt, and assignment hash. The coordinator records the exact observed
   completion response (or an explicit unavailable status after interruption)
   and then seals detailed observable execution under
   `.loopy_loop/traces/`.
7. The coordinator checks structural protocol evidence, session-local eval and
   control artifacts, child requests, and stop/budget conditions. Semantic
   quality remains the responsibility of the workflow/eval agents (D3/D4).

The `inner_outer_eval` template is organized around four workflows:

- `outer`: reviews implementation evidence, accepts or returns work, maintains
  the accepted ledger, and publishes eval-readiness context without closing
  the session.
- `inner`: dynamically plans/delegates and implements one focused unit in the
  target repo; its harness coordinator integrates all spawned-agent work.
- `eval_reviewer`: creates or refreshes outcome-oriented, session-scoped
  eval-banana checks.
- `eval_runner`: runs those checks, publishes the canonical eval receipt and
  matching `goal_check.json`, and alone may request successful terminal control
  for this layer.

The loop does not hide state inside a chat transcript. Continuity comes from
git plus compact files in `.loopy_loop/sessions/<session_id>/`; detailed,
prunable execution records live separately in `.loopy_loop/traces/`.

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
    │       ├── state.json
    │       ├── control.json
    │       ├── inputs/{user_updates.jsonl,accepted_request.json,artifacts/}
    │       ├── project_state/
    │       ├── eval_checks/
    │       ├── eval_readiness/
    │       ├── eval_receipts/
    │       ├── child_requests/{pending,accepted,rejected}/
    │       ├── child_outcomes/
    │       ├── parent_acceptance/
    │       ├── git_receipts/
    │       ├── delivery_receipts/
    │       ├── trace_seals/
    │       ├── iterations/
    │       └── children/<child_session_id>/...
    ├── traces/<root>/sessions/<session>/attempts/<attempt>/
    ├── trace_export_outbox/
    └── trace_finalization_outbox/
```

Workflow definitions are part of the repo and should usually be committed.
Session directories, traces, both outbox record types, and `repository.json` are
runtime output and are ignored by default. Session state/evidence is required
for recovery; traces may be pruned after sealing without removing that compact
truth.

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
- `model_tiers` (optional) declares named worker tiers — tier name → agent →
  `{model, effort}` — as the single source of truth for this repo's model
  ids. Loopy renders the table into the harness system prompt so coordinators
  can pass `spawn_agent(model=…)` to move one task to a different tier
  (guidance, not enforcement — see D8/D9 in `design/decisions.md`). The
  per-spawn `effort` argument was introduced in team-harness 0.4.0. Loopy 0.7
  requires team-harness 0.5.0 for the wider caller/trace contract, so current
  installs always have it. With `default_tier` set, the named tier
  derives `team_harness_agent_models` and
  `team_harness_agent_reasoning_efforts` (the tier must cover every
  configured agent); setting those mappings explicitly alongside
  `default_tier` is a config error.

  ```yaml
  model_tiers:
    strong:
      codex: {model: "gpt-5.6-sol", effort: "xhigh"}
      claude: {model: "claude-fable-5", effort: "max"}
    economy:
      codex: {model: "gpt-5.6-terra", effort: "low"}
      claude: {model: "claude-haiku-4-5"}
  default_tier: "economy"
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

Workflow config lives beside each workflow prompt:

```yaml
enabled: true
priority: 0
run_every: 1
must_follow: null
not_before_iteration: 0
run_on_start: false
run_after_successes: null
emits_goal_check: false
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

- `emits_goal_check=true` lets a non-`goal_check` workflow write
  `goal_check.json` as an eval artifact. Stopping still requires updating
  session `control.json`.

Each workflow set may declare `contract.yaml`. It names the layer kind, every
workflow role's responsibility, accountable state paths, eval author/runner/
goal-control roles, task-acceptance owner, terminal-blocker reporting roles,
and whether the set uses the recursive child interface. This is accountability
metadata and prompt context, not a filesystem ACL or semantic scheduler gate
(D8). All built-in templates declare `session_protocol_version: 2`. An older
custom set without a contract receives a conservative derived protocol-v1 role
contract and remains executable; add and validate an explicit v2 contract
before expecting evidence-bound terminal control and child requests.

## Output and Logging

Each fresh coordinator run creates a v2 root session. A workflow with a
recursive child interface may add nested child sessions under it; the active
path is still advanced by one loopy worker, depth first.

```text
.loopy_loop/sessions/<session_id>/
```

Session ids start with a UTC timestamp and include a deterministic goal hash, so
session directories sort chronologically and similar goals are easy to compare.

Important session files:

- `goal.md`: the exact goal text copied into the session.
- `goal_contract.json`: immutable, scoped completion/stop criteria,
  constraints, deliverables, and evidence expectations for this layer.
- `session.json`: immutable root/parent/depth/workflow identity and hashes.
- `workflow_contract.json`: role, ownership, eval-control, and child-interface
  declaration frozen for the session.
- `state.json`: revisioned coordinator-owned dispatch/recovery state, including
  the session's token/duration usage ledger.
- `events.jsonl`: append-only event stream — one versioned JSON line per
  significant transition (`session_started`, `task_dispatched`,
  `task_finished`, `iteration_abandoned`, `goal_check`, `child_started`,
  `child_finished`, `session_stopped`). Tail it with `loopy events --follow`.
- `control.json`: workflow-owned stop switch.
- `inputs/user_updates.jsonl`: append-only inputs, delivery records, and agent
  acknowledgements. Use `loopy update`; do not rewrite prior lines.
- `project_state/`: workflow-owned durable markdown state.
- `eval_checks/`: session-scoped eval-banana checks.
- `eval_readiness/`: task-acceptance/readiness context; it does not change
  scheduler eligibility.
- `eval_receipts/`: compact, session-bound eval verdicts and canonical reports.
- `child_requests/`, `child_outcomes/`, and `parent_acceptance/`: the typed
  parent/child handoff and separate parent disposition.
- `git_receipts/` and `delivery_receipts/`: compact evidence that must survive
  trace pruning.
- `control_rejected/` and `protocol_failures/`: preserved malformed v2
  terminal requests and the autonomous repair record.
- `iterations/`: immutable assignment/recovery artifacts for each loopy task.
- `inputs/`: child-local immutable copies of the accepted request and every
  declared parent input; the origin manifest separately preserves source refs.
- `trace_seals/`: compact hashes anchoring sealed/incomplete trace manifests.

Each iteration directory contains:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/
├── workflow_snapshot/
│   └── <attempt_id>/
│       ├── assignment.json
│       └── frozen workflow files
├── prompt.txt
├── result.json
├── result_text.txt
├── harness_run_id.txt
├── pending_finished_request.json
├── trace_ref.json
└── goal_check.json            # only for eval-emitting workflows
```

`workflow_snapshot/<attempt_id>/` freezes the selected workflow config, prompt,
role contract, root config snapshot, and their hashes. Its `assignment.json` binds that
snapshot to one repository/session/attempt and supplies absolute paths.
`prompt.txt` is the exact rendered input persisted before
`TeamHarness.run(...)`; `result.json` is the normalized mechanical result.
Recovery-critical files remain here even after detailed traces are pruned.

Detailed observable execution is routed to a separate, independently ignored
attempt trace:

```text
.loopy_loop/traces/<root_session_id>/sessions/<session_id>/attempts/<attempt_id>/
├── trace_manifest.json
├── protocol/      # task response, assignment, prompt, result, completion I/O
├── harness/       # canonical team-harness run and direct-agent records
├── agents/
├── eval/          # raw eval-banana output
├── git/           # verbose git evidence
└── service/       # coordinator-owned finished exchange/recovery record
```

Trace channels preserve the observable local bytes they receive; Loopy does
not inspect or redact values that resemble credentials. The trace tree is
gitignored by default, but it can contain prompts, outputs, environment-derived
data, binary artifacts, or other private material. Sealing hashes the raw local
artifacts; it is an integrity boundary, not a data-safety boundary. A channel
the provider or protocol does not expose is marked
unavailable/incomplete rather than invented. The coordinator captures the
exact `/finished` request and observed response before sealing. If state
acceptance committed but the response was interrupted, the service exchange
records that response as unavailable and the trace seals incomplete instead of
inventing it. Provider-native nested agents remain unavailable unless their CLI
exposes them. Direct team-harness spawns are complete only when every recorded
agent points to canonical local stdout/stderr files; a
built-in nested `type=harness` spawn must also have valid inherited loop/run
lineage and a recursively complete canonical run.

Operational commands are documented under [CLI Reference](#cli-reference).
Export currently means an idempotent exact copy to a local directory through
`.loopy_loop/trace_export_outbox/`. It applies no filtering. A future cloud
transport must own and declare its data-safety policy before sending any trace
off-host. Active or unsealed traces cannot be exported or pruned.

The binding v2 file/ownership contract is
[recursive-loop-layer-contract.md](design/designs/recursive-loop-layer-contract.md).
Legacy `harness_outputs/`, `updates_from_user.md`, and v1 session artifacts
remain readable on resume but are not the new write contract.

## Control and Completion

`control.json` is the session-scoped stop switch. A fresh session starts with a
neutral v1-compatible running record:

```json
{
  "state": "running",
  "reason": "session active",
  "stop_reason": null,
  "schema_version": 1
}
```

For a workflow set whose `contract.yaml` declares
`session_protocol_version: 2` (all packaged templates do), a terminal success
must be control v2 from the declared `goal_control_role` and must cite a valid
same-session eval receipt:

```json
{
  "schema_version": 2,
  "control_id": "control-unique-id",
  "state": "stopped",
  "reason": "evals passed",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "session-id",
    "workflow_id": "eval_runner",
    "attempt_id": "attempt-id"
  },
  "eval_receipt_ref": "session:/eval_receipts/eval-unique-id.json",
  "created_at": "2026-07-15T12:00:00Z"
}
```

The D5 last-resort terminal blocker does not need a passing eval, but it must
come from a role listed in `terminal_blocker_reporting_roles` and record the
autonomous routes already exhausted:

```json
{
  "schema_version": 2,
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

Both terminal forms must identify the exact session, workflow, and attempt in
the current task being completed. A prior attempt, another loop layer, or a
spawned agent cannot publish control for a later task to consume. A spawned
agent reports its conclusion to the harness coordinator; the accountable
current workflow publishes the layer-owned control record.

`goal_check.json` is a small per-iteration projection of the canonical eval
receipt. Its verdict and reason must match that receipt exactly:

```json
{
  "schema_version": 2,
  "goal_met": false,
  "reason": "docs still missing",
  "eval_receipt_ref": "session:/eval_receipts/eval-unique-id.json"
}
```

A valid `goal_check.json` does not stop the loop by itself. Before accepting
`goal_met`, the coordinator structurally validates the exact current control
producer, session/root/goal/attempt identity, every authored check and its
definition-byte hash, canonical report, and the receipt's one raw reference to
the producing attempt's canonical `eval/report.json`, including their hashes
and trace/harness identity. Authored checks are regular `*.yaml`/`*.yml` files
discovered recursively below the session's `eval_checks/`; symlinks and
non-files are rejected. Receipt JSON/schema failures retain field-qualified
diagnostics in failed history and terminal-control rejection evidence instead
of collapsing to a generic missing-receipt error. That singleton raw report is
required for failing as well as passing receipts. For a passing receipt the
coordinator also
verifies that it records the exact absolute project/output paths, a 1.0 passing
run, the same all-passed check inventory, zero judge exit codes, and matching
judge provider/model/reasoning effort. A live Git recapture must still match
the evaluated HEAD and `loopy-dirty-tree-v2-sha256` digest; that digest binds
the complete Git index as well as changed working-tree bytes, so partial staging
cannot alias another evaluated subject. These are provenance checks; the
coordinator does not second-guess the LLM judge's semantic conclusion
(D3/D4/D8).

Malformed v2 control is moved to `control_rejected/`, recorded in
`protocol_failures/`, reset to running, and exposed to later assignments for
repair. Repeated protocol breakage is bounded by the configured goal-check
failure cap. If goal-check output is missing or invalid repeatedly, the
coordinator stops with
`stop_reason="goal_check_broken"` after the configured failure cap. Similarly,
consecutive failed iterations of any single workflow stop the loop with
`stop_reason="workflow_failure_cap"` after `workflow_consecutive_failures_cap`.
Failed iterations record a `failure_kind` in history — `transient` (provider
said retry; team-harness's own retries were exhausted), `deterministic`
(auth/config errors retries cannot fix), `crash` (the task was abandoned by
worker-crash recovery; this does not prove an unverifiable worker died), or
`unknown` — so a stopped run is legible without reading harness logs.

Legacy session state, v1 child requests, and v1 terminal control remain
readable when resuming an existing v1 session. They retain their historical,
weaker provenance; they do not satisfy a packaged v2 workflow contract.

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

The v2 request carries an idempotency key, parent provenance, and a
child-scoped goal contract rather than copying the parent's broader criteria:

```json
{
  "schema_version": 2,
  "request_id": "feature-auth-1",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "attempt-id",
    "parent_work_item_id": "FEATURE-4",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Implement the selected authentication slice.",
    "completion_criteria": ["The child-scoped behavior passes evaluation"],
    "stop_criteria": ["A genuinely terminal blocker is established"],
    "constraints": [],
    "deliverables": ["code and verification evidence"],
    "required_evidence": ["eval, git, and delivery receipts"]
  },
  "inputs": []
}
```

The coordinator creates the child session under the parent session's
`children/` directory, freezes its own goal/workflow contract, runs the
requested workflow set, writes a factual `child_outcomes/<request_id>.json`
when it becomes terminal, and iteratively unwinds as many terminal ancestors as
needed. A child verdict never accepts or closes its parent. The accountable
parent role separately writes a receipt under `parent_acceptance/` after
reviewing integration evidence. Three active depths, two-level unwind, and
root-stop projection are covered by the v2 contract tests; there is no
hard-coded depth-two scheduler.

The reader observes both legacy flat `child_requests/*.json` and v2
`pending/*.json`. Valid v2 request bodies are preserved in `accepted/`;
invalid/undispatchable requests and reasons are preserved in `rejected/`.
Request ID, not filename, prevents duplicate dispatch. A corrupt v2
`children.json` is preserved under `protocol_failures/` and reconstructed only
from immutable accepted requests, child manifests, and child state; it is never
silently treated as empty.

The exact accepted request body is also copied to the child's immutable
`inputs/accepted_request.json`. Each declared `inputs[]` logical reference and
SHA-256 is resolved and checked from the parent's scope before dispatch, then
its exact bytes are copied under the child's `inputs/artifacts/`. The child goal
contract refers only to those local copies; its origin retains the parent source
refs, hashes, and mapping. The child's attempt assignment exposes each frozen
reference, hash, and worker-local absolute path, and the worker verifies them
again before invoking team-harness. Later parent state edits therefore cannot
change an already accepted child assignment.

The packaged `pm_planner_dispatcher` workflow set uses this contract for PM
orchestration:

- `planner` maintains PM state, selects one work item, reviews terminal child
  evidence, and owns parent-acceptance/eval-readiness receipts.
- `dispatcher` publishes the selected v2 child assignment and tracks factual
  lifecycle evidence without deciding acceptance.
- `eval_reviewer` and `eval_runner` evaluate the PM layer's own broader goal;
  only that layer's `eval_runner` may request successful terminal control.

## HTTP Contract

The coordinator exposes exactly two endpoints:

- `POST /register`
- `POST /finished`

Both return a `TaskResponse` with `action` equal to `"run"` or `"stop"`.

For a fresh v2 tree, `/register` requires worker protocol 2, the worker's
absolute repository root and stable repository ID, plus these capabilities:

```text
assignment_v1
frozen_workflow_v1
trace_manifest_v1
caller_run_record_v1
coordinator_input_v1
spawn_assignment_v1
nested_caller_context_v1
```

The last four are supplied by team-harness 0.5. Missing v2 protocol or
capabilities returns HTTP 426 without advancing state; a wrong checkout or
repository identity is refused. A `run` response carries the workflow/session/
iteration/attempt identity, frozen config and workflow snapshot, absolute
assignment path, repository identity, and required capabilities. A `stop`
response carries `stop_reason`.

`nested_caller_context_v1` does not create another loopy session. When the
harness coordinator uses the built-in `type=harness` spawn, team-harness keeps
the same root/current session, depth, workflow role, and loopy attempt; points
the nested coordinator at the direct agent's absolute assignment/output; and
records the parent harness-run ID. That nested coordinator remains accountable
to the outer harness coordinator for the same workflow assignment.

`/finished` must echo the exact worker owner, attempt, repository ID, and
assignment hash. A stale or mismatched response cannot complete current work.
It may receive the current scheduler response, but it never appends history,
creates or updates a trace-finalization intent, records a finished exchange, or
seals its stale attempt. Accepted v2 history binds the logical trace reference,
assignment hash, and hashes of the exact request and returned response.
If a worker exits after writing `result.json` and
`pending_finished_request.json` but before `/finished` is acknowledged, the
next `/register` recovers that completion exactly once instead of marking the
task abandoned.

The authoritative payload models are `RegisterRequest`, `TaskResponse`, and
`FinishedRequest` in
[`src/loopy_loop/models.py`](src/loopy_loop/models.py). The recursive ownership
and compatibility rules are in the binding design linked above.

## CLI Reference

```bash
loopy init [--template default|inner_outer_eval|pm_planner_dispatcher]
```

Scaffolds loopy-loop files. The default template creates only the reserved
`goal_check` workflow. `inner_outer_eval` creates the recommended outer/inner/eval
workflow set. `pm_planner_dispatcher` creates planner/dispatcher workflows for
child-session orchestration — and also ships the `inner_outer_eval` child set
its dispatcher spawns, so a clean init is executable end to end.

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
loopy status           # session stack, usage totals, estimated cost
loopy status --watch   # re-render every 2 seconds
loopy events           # the active session's event stream
loopy events --follow  # tail it live (--json for raw lines)
loopy update TEXT...   # append to the deepest active layer
loopy update --session SESSION_ID TEXT...
loopy stop             # tree-wide stop at the next safe boundary
loopy traces list
loopy traces inspect MANIFEST_OR_ID
loopy traces export MANIFEST_OR_ID --destination DIRECTORY
loopy traces prune MANIFEST_OR_ID
```

`status` prints the latest session state — the whole session stack while a
child runs (the live child is shown under its suspended parent), each
session's subtree token usage, and (with `model_prices` configured) estimated
cost. `update` appends the input record exactly as supplied; without `--session` it is
routed to the deepest active layer and later assignments append delivery and
acknowledgement records rather than editing history. `stop` projects root stop
intent through the whole active path and takes effect at the next register or
finish boundary; it does not invent a mid-harness interruption mechanism.

Trace commands accept a manifest ID, a trace root, or a manifest path confined
to this repository's `.loopy_loop/traces/`. `inspect` prints the manifest.
`export` creates/reuses a durable outbox entry and atomically publishes an
exact local copy without filtering. Reuse verifies both the outbox binding
and destination inventory; collisions or drift are refused. `prune` deletes
trace detail only. Export and pruning refuse active or unsealed traces, and
v2 pruning also requires the authentic session-plane seal receipt. Prune may
remove an authentically sealed trace after reporting later drift; export stays
strict about the sealed bytes. Neither removes compact session evidence. A
separate finalization outbox is written before a matching completion or
crash-abandonment state transition.
Startup acts on an entry only after durable history proves that exact attempt
committed, then retries sealing and any terminal child-outcome refresh. If the
completion committed before its HTTP response became durable, the trace records
the response as unavailable and seals incomplete. History and canonical trace
topology come from the hash-bound frozen assignment; an identity/hash mismatch
or invalid session topology leaves the outbox for repair instead of redirecting
the seal. A workflow-authored `sealed`/`incomplete` lifecycle without the
session-plane receipt is reopened, recorded as a protocol error, and resealed
incomplete by the coordinator. If the same attempt instead commits crash
abandonment, successful abandonment sealing removes its conflicting
uncommitted completion intent. An outbox I/O failure is
logged but does not roll back semantic state; trace storage is not an
acceptance gate.

## Related Projects

- [`team-harness`](https://github.com/writeitai/team-harness): the model and
  agent-CLI orchestration layer used by the loopy-loop worker.
- [`eval-banana`](https://github.com/writeitai/eval-banana): a lightweight YAML
  evaluation framework used by the packaged eval workflows. Installed
  automatically as a loopy-loop dependency.
