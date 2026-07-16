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
- additive `.gitignore` entries for session state, traces, the trace-finalization
  outbox, repository identity, and root state/lock/archive files

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
git plus compact files in `.loopy_loop/sessions/<session_id>/`; detailed
execution records live separately in `.loopy_loop/traces/`.

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
- `model_tiers` (optional) declares named worker tiers — tier name → agent →
  `{model, effort}` — as the single source of truth for this repo's model
  ids. Loopy renders the table into the harness system prompt so coordinators
  can pass `spawn_agent(model=…, effort=…)` to select one bundle for a task
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

Each coordinator run creates a root under `.loopy_loop/sessions/`. Recursive
workflow sets may create child sessions beneath it, but one worker still
advances only the deepest active session. Session files hold compact durable
truth: scoped goals, progress and decisions, task/recovery state, eval and
control receipts, child handoffs, and git/delivery evidence.

Each attempt freezes its workflow sources and `assignment.json`. The
assignment identifies the exact repository/session/workflow/attempt and gives
the harness coordinator absolute paths to its own state and outputs. Durable
receipts use logical `session:/`, `parent:/`, `root:/`, and `trace:` references
so a stopped checkout can move. Team-harness gives every direct spawn its own
absolute assignment/output paths and dynamic delegated task; the original
coordinator remains accountable for integrating the result.

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
Legacy v1 files remain readable on resume but are not the fresh-session write
contract.

## Control and Completion

Each session evaluates its own scoped goal. A delivery child may prove its task
while its parent still needs integration or release work. In the packaged
`inner_outer_eval` set, `outer` accepts task evidence, `eval_reviewer` authors
LLM-as-judge checks, and only `eval_runner` may publish successful terminal
control after a matching same-session eval receipt. Readiness informs prompts;
it does not become a semantic scheduler gate.

The one D5 escape hatch is an identity-bound v2 `unresolvable_error`. It is for
a genuinely terminal blocker after autonomous alternatives are exhausted:

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

Both successful and blocker control must identify the exact current
session/workflow/attempt. A delegate reports its conclusion to the harness
coordinator; it cannot publish a durable decision for another layer or a later
attempt. Invalid v2 control is archived with repair diagnostics instead of
being treated as semantic failure. Repeated broken protocol or workflow
execution is bounded by the configured failure caps.

The exact eval receipt, goal-check projection, successful control, blocker, and
rejection rules are in [docs/http-contract.md](docs/http-contract.md).

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

The v2 request carries parent provenance, a child-scoped goal contract, and
hashed input references. The coordinator archives the accepted request and
copies each verified input into the child's immutable `inputs/` area. Child
attempts receive those logical references, hashes, and absolute local paths;
later parent edits cannot change accepted work.

When the child stops, the engine writes a factual outcome. The parent then
writes a separate acceptance, rework, or reroute decision after reviewing the
evidence. Terminal descendants unwind iteratively, so the same edge supports
three or more active depths without a depth-specific scheduler. Invalid
requests are archived with reasons and can be repaired autonomously.

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

Fresh v2 registration binds worker protocol/capabilities to the absolute
checkout and stable repository ID. A missing capability returns HTTP 426
without advancing state. A run response identifies the frozen
session/workflow/attempt assignment; completion must echo its worker,
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
