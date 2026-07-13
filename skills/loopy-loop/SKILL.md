---
name: loopy-loop
description: Set up and run loopy-loop, an automation loop inside a repository that drives AI agents toward a goal across many iterations via a FastAPI coordinator and a single identity-verified worker. Use this skill when the user wants to install loopy-loop in a target repo, scaffold a workflow-set template, define workflows, configure workflow scheduling/cadence, use session-scoped project_state or eval checks, spawn child sessions (planner/dispatcher double loop), or operate the coordinator/worker pair (start, monitor, stop, resume, crash recovery). Also use it to migrate a pre-0.2.0 loopy-loop setup or to implement/debug a custom worker client against the coordinator HTTP contract.
---

# loopy-loop

`loopy-loop` runs an AI-agent improvement loop inside a target repository. Each
iteration runs one workflow via `team-harness`. A FastAPI coordinator owns all
loop state in files under `.loopy_loop/sessions/<session_id>/`; exactly **one**
worker executes assignments in a ping-pong over two endpoints: `POST /register`
gets the first task, `POST /finished` reports a result and receives the next
task. The worker does not poll. A second worker is refused (HTTP 409) while the
first is verifiably alive. Continuity lives in files and git state, never in a
chat transcript.

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

## Initialize the Target Repo

`cd` into the target repo, then pick a template:

```bash
loopy init                                    # minimal: goal_check only
loopy init --template inner_outer_eval        # recommended single loop
loopy init --template pm_planner_dispatcher   # double loop (parent + child sessions)
```

Idempotent — existing scaffold files are never overwritten (the one
exception is `.gitignore`, which is updated in place to ensure the sessions
ignore rule). Creates:

- `loopy_loop_config.yaml` — root config, edit this
- `loopy_loop_goal.txt` — the goal text (referenced by `goal_file` in config)
- `.loopy_loop/workflow_sets/<set>/workflows/<id>/{prompt.txt,config.yaml}`
  — one directory per workflow, grouped into named workflow sets
- `.gitignore` entry for `.loopy_loop/sessions/`

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
  review evidence) and `dispatcher` (spawn one child session per work item,
  import evidence back). Ships the `inner_outer_eval` set too — child sessions
  run it. This is the "double loop."

`goal_check` is a reserved workflow id. Don't rename or delete it in the
default template, and don't reuse the id for new workflows. It writes the
per-iteration `goal_check.json` eval artifact. Other workflows can also emit
`goal_check.json` by setting `emits_goal_check: true`. Note `goal_check.json`
is an eval artifact only — workflows stop the loop by updating the session
`control.json`.

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
team_harness_agent_models:
  codex: "gpt-5.5"
  claude: "claude-opus-4-8"
  gemini: "gemini-3.5-flash"
team_harness_agent_reasoning_efforts: {}
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
  `loopy_loop_goal.txt`). The resolved text — never the path — is what
  workflows and workers receive.
- **`workflow_set` is required** and must name a directory under
  `.loopy_loop/workflow_sets/`. `max_turns` is also required.
- `goal_hash` is derived from the goal text and used in session ids and session
  metadata; changing the goal starts a different session lineage.
- `team_harness_model` controls the harness coordinator agent. Use
  `team_harness_agent_models` to pin per-agent-type models.
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

```text
.loopy_loop/workflow_sets/<set>/
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
  workflow's rendered prompt. The required payload is exactly:

  ```json
  {"goal_met": false, "reason": "brief explanation", "schema_version": 1}
  ```

  `goal_met` (bool) and `reason` (string) are required; `schema_version`
  must be 1 when present. Any other shape counts as invalid goal-check
  output (fails the iteration and feeds the `goal_check_broken` cap). The
  workflow must still update session `control.json` if it wants the loop to
  stop.
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

## Session-Scoped State

Every rendered workflow prompt includes these path inputs (plus the goal
text, completion criteria, and stop criteria). The labels are exact; the
values below are schematic top-level examples — real rendered values are
absolute paths, and a child session's directories live nested under the
parent (`.loopy_loop/sessions/<parent_id>/children/<child_id>/...`, plus an
extra `Parent session directory:` line). Workflows must consume the rendered
values verbatim, never reconstruct them:

```text
Session directory: .loopy_loop/sessions/<session_id>
Session goal path: .loopy_loop/sessions/<session_id>/goal.md
Session project_state directory: .loopy_loop/sessions/<session_id>/project_state
Session eval_checks directory: .loopy_loop/sessions/<session_id>/eval_checks
Session updates_from_user path: .loopy_loop/sessions/<session_id>/updates_from_user.md
Session child_requests directory: .loopy_loop/sessions/<session_id>/child_requests
Session control path: .loopy_loop/sessions/<session_id>/control.json
Session finished ledger path: .loopy_loop/sessions/<session_id>/project_state/finished.md
Session harness outputs directory: .loopy_loop/sessions/<session_id>/harness_outputs
Iteration directory: .loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>
Iteration harness output root: .loopy_loop/sessions/<session_id>/harness_outputs/<NNNN>_<workflow_id>
```

**Prompts must treat the rendered Goal and the Session goal path as canonical**
— never the repo-root goal file. That file is not session-canonical: in a
child session it typically holds the parent's goal, and after
`loopy coordinator --goal-file ...` it does not match any session's goal.

The runtime only provides the paths; it does not parse markdown state. Put the
ownership rules in each workflow prompt. The packaged templates use:

- `outer` owns high-level planning, status transitions, and plan files
- `inner` implements exactly one available leaf task and marks it waiting for
  outer review
- `eval_reviewer` writes session-scoped eval-banana YAML checks under
  `eval_checks/`
- `eval_runner` runs only the session checks and writes `goal_check.json`

Useful `project_state/` conventions (workflow-owned, coordinator never parses):

```text
project_state/
├── README.md          # explains the state contract and file ownership
├── memory.md          # essential durable facts only
├── current_state.md   # live status, latest eval headline, next action
├── decisions.md       # accepted decisions with rationale
├── eval_results.md    # eval command/run/report index (owns eval detail)
├── finished.md        # outer-verified accepted completions only
└── what_we_should_do/
    └── plan.md
```

Outer workflows should read `updates_from_user.md` every run: it is the
human-writable inbox for requests that arrive mid-session. Reflect non-empty
content into `project_state/` first, then clear the file. Inner workflows must
not append to `finished.md`; the outer workflow owns verified completion.

Eval workflows run eval-banana against session-scoped checks:

```bash
eval-banana validate --cwd . --check-dir "<Session eval_checks directory>"
eval-banana run \
  --cwd . \
  --check-dir "<Session eval_checks directory>" \
  --output-dir "<Session directory>/eval_results"
```

substituting the rendered path values (they differ for child sessions), then
summarize and link the resulting `report.json` / `report.md` from
`project_state/eval_results.md`.

### Workflow-written control files must be published atomically

`control.json`, `goal_check.json`, and child request files are state-machine
inputs. Prompts should instruct agents to write a temp file in the same
directory and `mv` it over the final path. The failure modes differ:

- A torn `control.json` or `goal_check.json` is read as invalid output — it
  fails the iteration and, repeated, stops the loop (`goal_check_broken` /
  `invalid_control_output`).
- An unreadable or schema-invalid child request is renamed to an inspectable
  `*.json.rejected` file and skipped — the dispatch is silently lost while
  PM state may already claim `waiting_for_child`.

For child requests the temp filename must **not** end in `.json` (use e.g.
`item_042.json.tmp`): the coordinator dispatches any `*.json` file it sees.

## Child Sessions (double loop)

A parent workflow requests a child loop by writing a uniquely named `*.json`
file under the active session's `child_requests/` directory (only `*.json`
filenames are scanned — any other name is silently ignored forever):

```json
{
  "schema_version": 1,
  "workflow_set": "inner_outer_eval",
  "goal": "One concrete work item, phrased as a complete child goal"
}
```

The coordinator then suspends the parent, creates the child session (nested
under the parent's session directory), runs the child workflow set to a
terminal state, and resumes the parent with the child recorded in
`children.json`. One child at a time, and **one level only in v1**: only the
top-level session dispatches children. A child session writing its own
`child_requests/` is never dispatched — a nested workflow design that expects
grandchildren waits forever. The `pm_planner_dispatcher`
template packages this pattern: the **dispatcher** workflow owns
`child_requests/` (the planner never writes there), and the planner reviews
child evidence after the child terminates.

The session stack is durable: while a child runs, the parent's `state.json`
records `active_child_session_id`, and a restarted coordinator (`--resume`)
walks parent→child pointers to the deepest live session instead of orphaning a
running child.

## PR, Branch, and Merge Policy (for implementation workflows)

For implementation work that changes repo files, the default delivery path is:
create a branch, open a PR, wait for checks, and merge it.

Default to `PR expected: yes` and `Merge expected: yes` for implementation
tasks. Default both to `no` only for session-state-only, eval-only,
research-only, planning-only, or no-usable-remote/auth tasks.

Do not wait for a human for ordinary branch creation, PR creation, GitHub CLI
use, write permissions, or available auth. Do not merge when checks fail,
required review rules block merge, the merge would be destructive or monetary,
or the task explicitly says not to merge. If PR creation or merge is blocked,
record the exact blocker and remaining action in
`project_state/current_state.md`. Record delivery evidence (repo, branch, PR
URL, merge status, checks) in `project_state/finished.md`.

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

### Crash recovery (worker died mid-iteration)

When a new worker registers while a task is still marked live, the coordinator:

1. **Liveness check** — refuses (409) if the recorded worker is verifiably
   still alive on this host.
2. **Result recovery** — if the dead worker already produced
   `pending_finished_request.json` or `result.json`, the completed task is
   recorded; no work is lost.
3. **Orphan recovery** — otherwise the recovery policy is applied to agent
   processes the dead worker's harness run left behind: `drain` (default)
   waits up to `recovery_drain_timeout_s` for them to finish; `reap` kills
   them. When at least one orphaned run was actually handled, a
   `salvage.json` in the interrupted iteration directory records what
   happened to each orphan and the failed iteration carries
   `error="abandoned_after_<policy>"` (plain `"abandoned"` when nothing
   settled). The iteration is then re-dispatched unless a stop condition
   fires first (the abandonment consumes a turn, so it can itself trigger
   `max_turns`). If any orphan may still be running, the coordinator refuses
   to dispatch (409) rather than risk duplicate work.

A hung-but-alive worker keeps its task (409 names its pid); the escape hatch is
to kill that process and register again.

## Monitor and Stop

```bash
loopy status          # session stack, usage totals, estimated cost
loopy status --watch  # re-render every 2 seconds
loopy events          # the deepest active session's event stream
loopy events --follow # tail it live (--json for raw JSON lines)
loopy stop            # sets stop_requested=true; honored after the next /finished
```

`status` walks the durable session stack: while a child runs it shows the
live child under the suspended parent. Every session also has an append-only
`events.jsonl` (`session_started`, `task_dispatched`, `task_finished`,
`child_started`, `child_finished`, `session_stopped`, ...) — the operational
legibility stream (best-effort; the durable truth stays in state.json).

Both commands print a friendly error and exit if the coordinator holds the
state lock mid-request — retry shortly.

**Child-session caveat:** both commands operate on the latest **top-level**
session state. While a child session runs, `loopy status` shows the suspended
parent (often `current_task: none`), not the live child; and `loopy stop`
sets `stop_requested` on the parent — the child does not see the flag and
keeps iterating until it reaches a terminal state, and only then does the
resumed parent honor the stop.

Per-iteration artifacts live at
`.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/`
(`prompt.txt`, `result.json`, `result_text.txt`, `harness_run_id.txt`, plus
`goal_check.json` for emitting workflows). The workflow stop switch is the
session-root `control.json`:

```json
{"state": "stopped", "reason": "...", "stop_reason": "goal_met", "schema_version": 1}
```

`stop_reason` must be `goal_met` or `unresolvable_error`. `unresolvable_error`
is the loop's **last-resort** autonomous escape hatch: workflows should exhaust
re-scoping, retries, and routing around a blocker before using it — and the
`control.json` `reason` field itself must state the exact terminal blocker and
what autonomous alternatives were tried. A generic reason breaks the repo's
"make the give-up legible" contract (D5). Record the same blocker in
`project_state/current_state.md`.

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
- **Child request file not named `*.json`** → never scanned; the child is
  silently never dispatched.
- **Workflow id collision with `goal_check`** → reserved; pick a different id.
- **`must_follow` / `run_after_successes.workflow_id` references a missing
  workflow** → preflight fails; the id must match a folder in the same set.
- **Eval runner does not stop the loop** → confirm the workflow has
  `emits_goal_check: true`, writes valid JSON to the exact `goal_check.json`
  output path, and updates session `control.json` when the goal is met.
- **Child workflow prompts reading the repo-root goal file** → that file is
  never session-canonical (in a child session it typically holds the parent's
  goal). Prompts must use the rendered Goal / Session goal path.
- **A PM-only `team_harness_system_prompt_extension`** → it leaks into child
  sessions' system prompts. Keep it empty or set-neutral.
- **Non-atomic `control.json`/`goal_check.json` writes** → a torn file is
  invalid output; publish via temp file + rename.
- **Re-running `loopy coordinator` against a still-running state** →
  intentionally fatal. Pass `--resume` to attach.
- **Killing only the coordinator** → state stays `running`. Either pass
  `--resume` next time or `loopy stop` first to reach a terminal state.
- **Custom worker implementations** must send worker identity on `/register`
  (required since 0.3, else HTTP 400) **and on every `/finished`** — the
  `/finished` identity is what gets stamped onto the next dispatched task;
  omit it and liveness verification (the second-worker 409 protection)
  silently degrades to "unknown" after the first task. Echo the task's
  `attempt_id` on `/finished` (required since 0.4) or the completion is
  treated as stale, and supply a `starttime` token if verifiable same-host
  liveness is wanted.

## Reference

- HTTP contract: `docs/http-contract.md`
- Session layout: `docs/session-layout.md`
- Deliberate design decisions (do not "fix"): `design/decisions.md`
- Source: https://github.com/writeitai/loopy-loop
