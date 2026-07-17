---
name: loopy-loop
description: Set up and run loopy-loop, an automation loop inside a repository that drives AI agents toward a goal across many iterations via a FastAPI coordinator and a single identity-verified worker. Use this skill when the user wants to install loopy-loop in a target repo, scaffold a workflow-set template, define workflows, configure workflow scheduling/cadence, use session-scoped project_state or eval checks, spawn child sessions (planner/dispatcher double loop), or operate the coordinator/worker pair (start, monitor, stop, resume, crash recovery). Also use it to migrate a pre-0.2.0 loopy-loop setup or to implement/debug a custom worker client against the coordinator HTTP contract.
---

# loopy-loop

`loopy-loop` runs an AI-agent improvement loop inside a target repository. Each
iteration runs one workflow via `team-harness`. A FastAPI coordinator owns the
engine state in files under `.loopy_loop/sessions/<session_id>/`; workflow
roles own the semantic state and evidence declared by their contract. Exactly
**one** worker executes assignments in a ping-pong over two endpoints:
`POST /register` gets the first task, `POST /finished` reports a result and
receives the next task. The worker does not poll. A second worker is refused
(HTTP 409) while the first is verifiably alive. Continuity lives in files and
git state, never in a chat transcript.

Use this skill when the user asks to:

- Install loopy-loop or scaffold it in a repo (`loopy init`, templates)
- Configure a goal file, completion criteria, model, or workflow set
- Configure workflow cadence (`priority`, `must_follow`, `run_on_start`,
  `run_after_successes`) or eval-driven stopping (`emits_goal_check`)
- Design workflow prompts that use session-scoped `project_state/` and
  `eval_checks/`
- Run a planner/dispatcher parent loop that spawns child sessions
- Start, monitor, stop, resume, or crash-recover the loop

## Install

For end-user use in a target repository, install as a tool:

```bash
uv tool install loopy-loop
```

For development against a checkout of the loopy-loop repo:

```bash
uv sync --all-extras
```

The CLI exposes both `loopy` and `loopy-loop` — prefer `loopy`. The
`eval-banana` CLI (used by the packaged eval workflows) installs automatically
as a loopy-loop dependency; the worker makes it visible to spawned agents even
under `uv tool install`.

The recursive v2 contract requires the companion releases that implement its
cross-project boundaries: `team-harness>=0.5.0` for caller-owned traces and
per-spawn assignment envelopes, and `eval-banana>=0.3.5` for hermetic eval
execution, canonical check-definition digests, exact judge inputs, and
collision-safe per-check artifacts. A fresh v2 worker advertises these
capabilities; the coordinator rejects an older worker before advancing state.

## Initialize the Target Repo

`cd` into the target repo, then pick a template:

```bash
loopy init                                    # minimal: goal_check only
loopy init --template inner_outer_eval        # recommended single loop
loopy init --template pm_planner_dispatcher   # double loop (parent + child sessions)
```

Idempotent — existing scaffold files are never overwritten (the one
exception is `.gitignore`, which is updated in place to ensure all runtime
ignore rules). Creates:

- `loopy_loop_config.yaml` — root config, edit this
- `loopy_loop_goal.txt` — the goal text (referenced by `goal_file` in config)
- `.loopy_loop/workflow_sets/<set>/contract.yaml` — the layer and role
  accountability contract
- `.loopy_loop/workflow_sets/<set>/workflows/<id>/{prompt.txt,config.yaml}`
  — one directory per workflow, grouped into named workflow sets
- additive `.gitignore` entries for session state, traces, the
  trace-finalization outbox, repository identity, and root state/lock/archive
  files

Templates:

- **default**: a single `goal_check` workflow in workflow set `main`. A
  starting point for fully custom workflow sets — **not runnable as-is**:
  `goal_check` is never eligible until some non-`goal_check` workflow has
  succeeded, so a bare default init stops immediately with
  `no_eligible_workflow`. Add at least one implementation workflow first;
  the packaged templates below are the runnable walkthroughs.
- **inner_outer_eval**: `outer` (plan/review), `inner` (implement),
  `eval_reviewer` (author session-scoped eval-banana checks), `eval_runner`
  (run checks, write `goal_check.json`). The recommended general-purpose loop.
- **pm_planner_dispatcher**: `planner` (maintain PM state, pick work items,
  review evidence), `dispatcher` (spawn one child session per work item and
  import factual outcomes), and parent-layer `eval_reviewer`/`eval_runner`
  roles that evaluate the broader PM goal. Ships the `inner_outer_eval` set
  too — child sessions run it. This is the "double loop."

`goal_check` is a reserved workflow id. Don't rename or delete it in the
default template, and don't reuse the id for new workflows. It writes the
per-iteration `goal_check.json` eval artifact. Other workflows can also emit
`goal_check.json` by setting `emits_goal_check: true`. In a fresh v2 workflow
set, that file is a receipt-bound projection of the current layer's eval
verdict. It is not a stop switch; only an identity-bound session
`control.json` can request a workflow-authored terminal stop for the layer.
Engine-owned limits and operator stop intent can also end a run.

## Configure

### Root config — `loopy_loop_config.yaml`

```yaml
goal_file: "loopy_loop_goal.txt"
workflow_set: "main"
completion_criteria:
  - "Homepage renders without errors"
stop_criteria:
  - "A workflow updates session control.json to stopped"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "codex"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
  - "claude"
  - "gemini"
model_tiers:
  strong:
    codex: {model: "gpt-5.6-sol", effort: "xhigh"}
    claude: {model: "claude-fable-5", effort: "max"}
    gemini: {model: "gemini-3.5-pro"}
  economy:
    codex: {model: "gpt-5.6-terra", effort: "low"}
    claude: {model: "claude-haiku-4-5"}
    gemini: {model: "gemini-3.5-flash"}
default_tier: "economy"
# Optional coordinator retry controls. Omit to use team-harness defaults.
# team_harness_max_retries: 8
# team_harness_retry_base_delay_s: 2.0
# team_harness_retry_max_delay_s: 60.0
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
team_harness_system_prompt_extension: ""
# Coordinator-side crash recovery (not sent to workers):
# recovery_policy: "drain"          # or "reap"; drain is the default
# recovery_drain_timeout_s: 600.0
# Per-workflow circuit breaker (coordinator-side, not sent to workers):
# workflow_consecutive_failures_cap: 5
# Optional cost budget (coordinator-side; prices are USD per 1M tokens for
# the harness coordinator model — agent-CLI spend is not measurable):
# model_prices:
#   prompt_usd_per_1m: 2.5
#   completion_usd_per_1m: 10.0
# max_cost_usd: 50.0
```

Constraints:

- **`goal_file` is required; an inline `goal:` key is rejected** with a config
  error. The goal text lives in the referenced file (default
  `loopy_loop_goal.txt`). The resolved text is frozen into the session, and
  each v2 attempt also receives absolute paths to that current session's
  canonical `goal.md` and `goal_contract.json`.
- **`workflow_set` is required** and must name a directory under
  `.loopy_loop/workflow_sets/`. `max_turns` is also required.
- `goal_hash` is derived from the goal text and used in session ids and session
  metadata; changing the goal starts a different session lineage.
- `team_harness_model` controls every harness coordinator at every session
  depth; keep it uniformly strong. `model_tiers` bundle each direct-agent
  model and effort under a capability name. With `default_tier` set, it derives
  the per-agent defaults and must cover every configured agent. Do not also set
  `team_harness_agent_models` or `team_harness_agent_reasoning_efforts` in that
  mode. Coordinators choose named tiers per spawn as prompt-guided, audited
  judgment; the engine does not enforce the choice.
- `team_harness_api_base` is normalized: trailing slash stripped, `/v1`
  appended when missing — write whichever form you prefer.
- `team_harness_system_prompt_extension` applies to **every** harness run in
  the repo — including child sessions spawned by a parent workflow set. Keep it
  empty or strictly workflow-set-neutral; a parent-only instruction (e.g. "do
  not implement directly") would reach child implementers at system-prompt
  level and contradict their job.
- `recovery_policy` / `recovery_drain_timeout_s` are coordinator-side only:
  they control what happens to agent processes a crashed worker left behind
  (`drain` = wait bounded, let them finish; `reap` = kill). They are not part
  of the config snapshot sent to workers.
- `workflow_consecutive_failures_cap` (default 5, coordinator-side only): that
  many consecutive failed iterations of the same workflow stop the loop with
  `stop_reason="workflow_failure_cap"` instead of retrying a wedged workflow
  until `max_turns`; any success of that workflow resets its counter. Failed
  iterations carry a `failure_kind` in history (`transient` / `deterministic`
  / `crash` / `unknown`) so a stopped run is legible.
- Unknown config keys are rejected. All `team_harness_*` field names are exact.
- The env var named in `team_harness_api_key_env` must be exported in the shell
  that starts the coordinator AND the shell that starts the worker.
- Some providers (e.g. `codex`) skip the API-key preflight check.

### Workflows — `.loopy_loop/workflow_sets/<set>/workflows/<id>/`

Workflows are grouped into named **workflow sets**; the root config's
`workflow_set` (or `loopy coordinator --workflow-set`) selects which set runs.
Each workflow is a folder; the folder name is the workflow id.

Each current set also declares `contract.yaml`. It names the layer kind, every
workflow role and responsibility, accountable semantic state, eval author and
runner, successful-control owner, task-acceptance owner, terminal-blocker
reporters, and whether the recursive child interface is available. The
contract is frozen into every v2 assignment. It expresses accountability and
prompt context, not a filesystem ACL or semantic scheduler veto. A custom set
with no contract uses the conservative legacy-v1 compatibility path; add and
validate an explicit contract before expecting v2 child, eval, and control
artifacts.

```text
.loopy_loop/workflow_sets/<set>/
├── contract.yaml             # layer and role accountability
└── workflows/
    └── <id>/
        ├── prompt.txt        # the prompt the workflow runs
        └── config.yaml       # scheduling
```

`config.yaml` (all fields optional; defaults shown):

```yaml
enabled: true
run_every: 1
must_follow: null
not_before_iteration: 0
priority: 0
run_on_start: false
run_after_successes: null
emits_goal_check: false
description: ""
```

Rules:

- `must_follow` must resolve to an existing workflow in the same set during
  coordinator preflight.
- `run_every` counts completed iterations, not wall-clock time.
- `priority` breaks ties among eligible workflows; higher values run first.
- `run_on_start: true` makes a workflow eligible before any successful workflow
  has run.
- `run_after_successes` makes a workflow eligible after every N successful runs
  of another workflow:

```yaml
run_after_successes:
  workflow_id: inner
  every: 10
```

- `emits_goal_check: true` adds a `goal_check.json` output path to that
  workflow's rendered prompt. For a fresh v2 contract, it must project a
  canonical same-session eval receipt:

  ```json
  {
    "schema_version": 2,
    "goal_met": false,
    "reason": "the exact receipt verdict reason",
    "eval_receipt_ref": "session:/eval_receipts/eval-<id>.json"
  }
  ```

  An already-running legacy v1 session retains its historical compact
  `goal_met`/`reason` projection. Do not emit that form for a v2 assignment.
  Missing, malformed, or mismatched output fails the iteration and feeds the
  `goal_check_broken` cap. A valid projection still does not stop a session;
  v2 successful control must cite the same passing receipt.
- An iteration counts as **successful when the harness run completed**, not
  when its work was good — quality judgment belongs to eval workflows (a
  deliberate design decision; see `design/decisions.md` D3 in the source repo).

Example cadence for an outer/inner loop with periodic evals:

```yaml
# eval_reviewer/config.yaml
enabled: true
priority: 100
run_on_start: true
run_after_successes:
  workflow_id: inner
  every: 10
description: "Create or refresh eval checks."
```

```yaml
# eval_runner/config.yaml
enabled: true
priority: 90
must_follow: eval_reviewer
run_after_successes:
  workflow_id: inner
  every: 10
emits_goal_check: true
description: "Run eval checks and update session control when they pass."
```

```yaml
# outer/config.yaml
enabled: true
priority: 10
description: "Review state and plan the next task."
```

```yaml
# inner/config.yaml
enabled: true
priority: 20
must_follow: outer
not_before_iteration: 1
description: "Implement the next planned leaf task."
```

This sequence starts with `eval_reviewer`, then repeats `outer -> inner`. After
10 successful `inner` runs, `eval_reviewer -> eval_runner` becomes eligible.

## Assignment-First Session Contract

Every fresh packaged workflow uses session protocol v2. At the start of an
attempt, read the absolute `assignment.json` path near the top of the rendered
prompt, then read the frozen workflow contract it references, before reading
or writing any state. Do not reconstruct a session path from the repository
root, a familiar filename, or a previous attempt.

The frozen assignment identifies the root, current, and parent sessions; depth,
layer kind, workflow, role, iteration, and attempt; the current layer's scoped
goal; the role's responsibility and expected outputs; accepted child inputs;
and the relevant absolute state, evidence, output, contract, and trace paths.
Its referenced frozen workflow contract contains the complete per-role state,
eval, acceptance, blocker, and child-interface accountability. Absolute paths
are execution-time capabilities in this checkout. Durable evidence links use
confined `repo:/`, `session:/`, `parent:/`, `root:/`, `session:<id>:/`, and
`trace:<id>:/` references so the session remains inspectable after the
checkout moves.

`goal.md` and `goal_contract.json` in the assigned current session are the
canonical target for that layer. Never substitute the repo-root goal file: a
child has a narrower goal, and a `--goal-file` override is already frozen into
the session.

Team-harness creates a separate absolute `agent_assignment.json` and output
directory for every direct spawn. A direct agent performs only its dynamically
delegated task and reports back to the harness coordinator. That coordinator
owns integration and only the outputs assigned to its current workflow role;
the contract's other roles retain their acceptance, eval, and control
responsibilities. A nested `type=harness` spawn is another coordinator inside
the same attempt and session layer; only a loopy child request creates a new
durable loop layer.

## Durable State, Evidence, and Raw Traces

Keep two retention planes distinct:

- `.loopy_loop/sessions/` holds compact durable truth needed to schedule,
  recover, evaluate, and justify acceptance: goals, engine state, frozen
  assignments, semantic progress and decisions, child handoffs, eval receipts,
  git/delivery receipts, normalized results, and trace seals.
- `.loopy_loop/traces/` holds detailed observable attempt I/O: exact rendered
  prompts, team-harness coordinator input and run records, per-spawn
  assignments, stdout/stderr, raw eval output, verbose git evidence, provider
  and process identity, timing, and usage.

Both are gitignored runtime output. Session state must survive while a run is
active; raw traces are independently retainable, sensitive local data. A
sealed trace proves the local inventory and channel completeness, not semantic
success. Correct scheduling and acceptance never depend on retaining raw trace
bytes. “All I/O” means observable or model-visible logical input and output;
hidden reasoning and provider-internal transport are marked unavailable, not
inferred.

Within one session:

- `state.json` is coordinator-owned engine state and read-only to agents.
- `project_state/` is workflow-owned semantic progress and decisions;
  `project_state/finished.md` contains only work accepted by the contract's
  task-acceptance role.
- `inputs/user_updates.jsonl` is the append-only v2 operator-input journal.
  `loopy update` routes to a named session or the deepest active layer;
  attempts receive pending records and append acknowledgement records instead
  of editing history. `updates_from_user.md` is legacy compatibility only.
- `eval_checks/`, `eval_readiness/`, and `eval_receipts/` belong to the
  current layer's declared eval and acceptance roles.
- `child_requests/`, `child_outcomes/`, and `parent_acceptance/` separate
  dispatch facts from the parent's later semantic decision.
- `git_receipts/`, `delivery_receipts/`, and `trace_seals/` retain compact
  evidence independently from raw traces.

The engine does not parse arbitrary markdown state or enforce path ACLs.
Ownership is accountability: prompts and assignments name the responsible
role, while eval and evidence detect violations and provide a repair path.

## Evaluation and Terminal Control

Every session evaluates its own scoped goal. A passing child result is evidence
for its parent, never proof that the parent's broader integration or program
goal is complete. In the stock `inner_outer_eval` contract:

- `outer` reviews task evidence, records acceptance, and publishes readiness;
- `eval_reviewer` authors only outcome-oriented `harness_judge` checks under
  the assigned `eval_checks/` directory; and
- `eval_runner` runs the complete current-layer inventory, writes the canonical
  receipt, projects it into `goal_check.json`, and alone may request
  `goal_met` control.

Run eval-banana hermetically against the exact assigned paths and place raw
output in the attempt's assigned trace `eval/` directory:

```bash
eval-banana validate --no-project-config --cwd "<assigned repo_root>" \
  --check-dir "<assigned eval_checks path>" --harness-agent "<declared judge agent>"
eval-banana run --no-project-config --flat-output \
  --cwd "<assigned repo_root>" --check-dir "<assigned eval_checks path>" \
  --output-dir "<assigned raw_eval_output path>" --pass-threshold 1.0 \
  --harness-agent "<declared judge agent>" --harness-model "<declared judge model>" \
  --harness-reasoning-effort "<declared effort>"
loopy capture-git-receipt --repo-root "<assigned repo_root>" \
  --attempt-id "<current attempt id>" \
  --output "<assigned git_receipts>/git-after-<current attempt id>.json"
```

Use the exact judge settings and optional timeout declared by the frozen
workflow prompt; do not rely on project or ancestor configuration.

The durable receipt under `eval_receipts/` binds the current session and goal,
producer attempt and harness run, evaluated git state, exact check inventory,
eval-banana's canonical definition digests, judge settings, and canonical/raw
report hashes. The iteration-local projection is schema v2:

```json
{
  "schema_version": 2,
  "goal_met": true,
  "reason": "All declared checks passed.",
  "eval_receipt_ref": "session:/eval_receipts/eval-<id>.json"
}
```

`goal_check.json` is not a stop switch. Successful terminal control must cite
that same passing receipt and identify the exact producing attempt:

```json
{
  "schema_version": 2,
  "control_id": "control-<unique-id>",
  "state": "stopped",
  "reason": "The session-scoped eval passed.",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "<current-session-id>",
    "workflow_id": "eval_runner",
    "attempt_id": "<current-attempt-id>"
  },
  "eval_receipt_ref": "session:/eval_receipts/eval-<id>.json",
  "attempted_routes": [],
  "evidence_refs": [],
  "created_at": "<UTC timestamp>"
}
```

For D5's last-resort `unresolvable_error`, omit the eval receipt, use a role
declared as a terminal-blocker reporter, and include at least one specific
autonomous route already attempted. A direct spawn reports a conclusion to its
harness coordinator; it never publishes durable control for another role,
attempt, or layer. Invalid v2 control is archived with repair diagnostics and
restored to running; there is no paused or waiting-for-human state.

Publish `control.json`, `goal_check.json`, eval receipts, and child requests by
writing a uniquely named same-directory temporary file and atomically renaming
it over the final path. A child-request temporary filename must not end in
`.json`, because every regular pending `*.json` is eligible for dispatch.

## Recursive Child Sessions

A workflow creates one sequential child by atomically publishing a unique v2
request to its assignment's absolute `child_requests` path. In a v2
assignment, that key already resolves to the current session's
`child_requests/pending/` directory:

```json
{
  "schema_version": 2,
  "request_id": "feature-auth-1",
  "workflow_set": "inner_outer_eval",
  "origin": {
    "parent_attempt_id": "<current-attempt-id>",
    "parent_work_item_id": "FEATURE-4",
    "supersedes_request_id": null
  },
  "assignment": {
    "goal": "Implement the selected authentication slice.",
    "completion_criteria": ["The child-scoped outcome passes evaluation"],
    "stop_criteria": ["A genuinely terminal blocker is established"],
    "constraints": [],
    "deliverables": ["code and verification evidence"],
    "required_evidence": ["eval, git, and delivery receipts"]
  },
  "inputs": [
    {
      "ref": "session:/project_state/dispatch_inputs/feature-auth-1.json",
      "sha256": "sha256:<64 hex characters>"
    }
  ]
}
```

If the selected planning record is mutable, freeze an immutable per-request
snapshot first, hash that snapshot into the request, atomically publish the
request, and only then update the mutable ledger. The stock dispatcher uses
`project_state/dispatch_inputs/<request_id>.json`; hashing `work_items.md`
would make the required `waiting_for_child` update invalidate the request.

The coordinator validates and archives the accepted request, copies every
verified input into the child's immutable `inputs/` area, suspends the parent,
and creates the nested child session. Only the deepest live session receives a
loopy assignment; every ancestor is suspended on one child. The same edge is
recursive, so one-loop, planner/dispatcher double-loop, and three-or-more-layer
systems use one state machine with no depth-specific scheduler.

When a child becomes terminal, the engine writes a factual
`child_outcomes/<request_id>.json` and resumes the parent. The accountable
parent role separately records acceptance, rework, or reroute; child
`goal_met` never closes an ancestor automatically. Invalid requests are moved
to `child_requests/rejected/` with reasons and can be repaired with a new
request identity. On `--resume`, the coordinator reconstructs the active path
from durable parent/child pointers and continues at the deepest live layer.

## Git, Branches, PRs, and Delivery

Follow the scoped goal, frozen workflow contract, and assignment's declared
deliverables. For implementation work that requires hosted delivery, the usual
path is a focused branch, PR, required checks, and merge. Planning, research,
eval-only, and session-state-only assignments normally do not invent a PR.

The worker captures compact git boundaries automatically. The accountable
workflow role records branch, commit, PR, CI, merge, blockers, and remaining
actions in `delivery_receipts/`; verbose status and diffs stay in the trace. A
direct agent may implement or inspect delivery, but its harness coordinator
must integrate and verify the result. Only the contract's task-acceptance role
links accepted delivery evidence into `project_state/finished.md`.

Do not wait for a human for ordinary branch creation, GitHub CLI use, PR
creation, or available repository authentication. Repair failed checks and
autonomously route around ordinary delivery problems. Use D5's terminal
`unresolvable_error` only when a required delivery step is genuinely impossible
after concrete alternatives have been exhausted. The engine records delivery
facts but does not impose a branch or PR as a preventive scheduling gate.

## Run

Two processes; start in separate terminals.

```bash
# terminal 1 — coordinator
export OPENROUTER_API_KEY=...            # whatever team_harness_api_key_env names
loopy coordinator --host 127.0.0.1 --port 8080
```

```bash
# terminal 2 — the single worker
export OPENROUTER_API_KEY=...
loopy worker --coordinator http://127.0.0.1:8080
```

- The coordinator accepts `--workflow-set` (override the config's set) and
  `--goal-file` (copy a different goal into the new session).
- **Exactly one worker.** The worker sends its process identity (hostname, pid,
  start-time token) on `/register`; while a registered worker is verifiably
  alive, a second `/register` is refused with HTTP 409 (the worker prints the
  refusal and exits with code 3). Identity is captured automatically — nothing
  to configure.
- `/register` may legitimately block for minutes after a crash: the coordinator
  first recovers the previous worker's interrupted iteration (see Crash
  recovery below) before dispatching fresh work.

### Fresh start vs resume

On startup the coordinator inspects the latest session's state:

- Terminal state → archived to `state.json.archive_<timestamp>.json`, fresh session.
- Still `running` → startup fails unless `--resume` is passed.

```bash
loopy coordinator --resume
```

Use `--resume` when reattaching after the coordinator died without reaching a
terminal state. Resume reconstructs the session stack: a running child session
continues where it was; a child found terminal is finalized and its parent
resumed.

### Crash recovery (worker task interrupted)

When a new worker registers while a task is still marked live, the coordinator:

1. **Liveness check** — refuses (409) if the recorded worker is verifiably
   still alive on this host.
2. **Result recovery** — if the prior worker already produced
   `pending_finished_request.json` or `result.json`, the completed task is
   recorded; no work is lost.
3. **Orphan recovery** — otherwise the recovery policy is applied to agent
   processes the prior worker's harness run left behind: `drain` (default)
   waits up to `recovery_drain_timeout_s` for them to finish; `reap` kills
   them. When at least one harness run is processed, `salvage.json` records
   what happened; the failed iteration carries
   `error="abandoned_after_<policy>"` only when something settled (plain
   `"abandoned"` otherwise). The abandonment consumes a turn and normal
   scheduling continues only if no stop condition fires; the same workflow is
   not guaranteed to run next. For identity-tracked harness runs, an unsettled
   reaper outcome refuses dispatch (409). A remote loopy worker skips reaping
   and falls back to legacy abandonment; when local worker liveness is
   unverifiable, same-host team-harness recovery is still attempted if its run
   records support it. Those fallback paths cannot provide the local safety
   proof.

A hung-but-alive worker keeps its task (409 names its pid); the escape hatch is
to kill that process and register again.

## Monitor and Stop

```bash
loopy status          # session stack, usage totals, estimated cost
loopy status --watch  # re-render every 2 seconds
loopy events          # the deepest active session's event stream
loopy events --follow # tail it live (--json for raw JSON lines)
loopy update TEXT...  # append input to the deepest active layer
loopy update --session SESSION_ID TEXT...
loopy stop            # tree-wide stop at the next safe register/finish boundary
loopy traces list
loopy traces inspect MANIFEST_OR_ID
```

`status` walks the durable session stack: while a child runs it shows the
live child under the suspended parent. Every session also has an append-only
`events.jsonl` (`session_started`, `task_dispatched`, `task_finished`,
`child_started`, `child_finished`, `session_stopped`, ...) — the operational
legibility stream (best-effort; the durable truth stays in state.json).

`stop` records root intent and projects it through the active path. The deepest
layer sees it at the next coordinator check-in; loopy deliberately does not
invent a second mid-harness interruption protocol. `status` and `events`
resolve the same durable stack, so a live child appears under its suspended
ancestors.

Trace commands accept a manifest ID, trace root, or manifest path confined to
this repository. `inspect` reports the manifest and current integrity. A
workflow or provider failure can leave a trace correctly sealed as
`incomplete`; that is observability, not a semantic verdict.

The commands print a friendly error and exit if the coordinator holds the
state lock mid-request — retry shortly. Per-iteration recovery artifacts live
under the assigned session's `iterations/<NNNN>_<workflow_id>/`; detailed
attempt data lives under the assignment's canonical trace root. Inspect the
assignment or `docs/session-layout.md` instead of guessing either absolute
path.

If `goal_check.json` is repeatedly missing or invalid, the coordinator stops
with `stop_reason="goal_check_broken"` after
`goal_check_consecutive_failures_cap` consecutive failures. Consecutive failed
iterations of any single workflow similarly stop the loop with
`stop_reason="workflow_failure_cap"` after
`workflow_consecutive_failures_cap` (default 5).

## Common Pitfalls

- **Inline `goal:` in the root config** → rejected. Use `goal_file:` pointing
  at a text file (default `loopy_loop_goal.txt`).
- **Missing `workflow_set` or `max_turns`** → both are required root-config
  fields.
- **API key not exported** → coordinator preflight fails. Export the env var
  named in `team_harness_api_key_env` in both shells.
- **Unknown config field** → parser rejects it. `team_harness_*` field names
  are exact; check spelling.
- **Starting a second worker** → HTTP 409 / exit code 3 while the first is
  verifiably alive. One worker per coordinator, by design.
- **Old workflows layout** → `.loopy_loop/workflows/<id>/` is not loaded.
  Workflows live under `.loopy_loop/workflow_sets/<set>/workflows/<id>/`.
- **Bare default template with no added workflow** → stops immediately with
  `no_eligible_workflow` (`goal_check` alone is never eligible first).
- **Child request published outside `child_requests/pending/` or not named
  `*.json`** → not a fresh v2 request. Use the exact assigned pending path and
  a non-`.json` temporary filename before atomic rename.
- **Workflow id collision with `goal_check`** → reserved; pick a different id.
- **`must_follow` / `run_after_successes.workflow_id` references a missing
  workflow** → preflight fails; the id must match a folder in the same set.
- **Eval runner does not stop the loop** → confirm the workflow has
  `emits_goal_check: true`, the complete session inventory has a valid
  canonical receipt, `goal_check.json` projects that receipt, and schema-v2
  `control.json` cites it from the exact current eval-runner attempt.
- **Child workflow prompts reading the repo-root goal file** → that file is
  never session-canonical (in a child session it typically holds the parent's
  goal). Read the assignment, then the assigned current-session `goal.md` and
  `goal_contract.json`.
- **A PM-only `team_harness_system_prompt_extension`** → it leaks into child
  sessions' system prompts. Keep it empty or set-neutral.
- **Non-atomic `control.json`/`goal_check.json` writes** → a torn file is
  invalid output; publish via temp file + rename.
- **Direct spawn writes layer control or parent acceptance** → the spawn is a
  delegate, not a durable role owner. It reports through its assigned output;
  the current harness coordinator integrates and publishes accountable state.
- **Re-running `loopy coordinator` against a still-running state** →
  intentionally fatal. Pass `--resume` to attach.
- **Killing only the coordinator** → state stays `running`. Either pass
  `--resume` next time or `loopy stop` first to reach a terminal state.
- **Custom worker implementations** must follow the complete v2 handshake in
  `docs/http-contract.md`: advertise protocol/capabilities and repository
  identity, send worker identity on `/register` and `/finished`, verify the
  frozen assignment and trace, and echo the exact attempt and assignment hash.
  A stale or mismatched completion cannot mutate current work.

## Reference

- HTTP contract: `docs/http-contract.md`
- Session layout: `docs/session-layout.md`
- Deliberate design decisions (do not "fix"): `design/decisions.md`
- Source: https://github.com/writeitai/loopy-loop
