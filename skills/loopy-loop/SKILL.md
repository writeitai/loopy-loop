---
name: loopy-loop
description: Set up and run loopy-loop, a repo-local automation loop that drives AI agents toward a goal across many iterations via a FastAPI coordinator and one or more workers. Use this skill when the user wants to install loopy-loop in a target repo, scaffold its config, define workflows, configure workflow scheduling/cadence, use session-scoped project_state or eval checks, or operate the coordinator/worker pair (start, monitor, stop, resume).
---

# loopy-loop

`loopy-loop` runs an AI-agent improvement loop inside a target repository. Each
iteration runs one workflow via `team-harness`. A FastAPI coordinator owns loop
state in `.loopy_loop/state.json`; one or more blocking workers poll it over
HTTP and execute the assigned workflow.

Use this skill when the user asks to:

- Install loopy-loop or scaffold it in a repo (`loopy init`)
- Configure a goal, completion criteria, model, or workflow
- Configure workflow cadence (`priority`, `must_follow`, `run_on_start`,
  `run_after_successes`) or eval-driven stopping (`emits_goal_check`)
- Design workflow prompts that use session-scoped `project_state/` and
  `eval_checks/`
- Start, monitor, stop, or resume the loop

## Install

For end-user / repo-local use, install as a tool:

```bash
uv tool install loopy-loop
```

For development against a checkout of the loopy-loop repo:

```bash
uv sync --extra dev
# or
uv pip install .
```

The CLI exposes both `loopy` and `loopy-loop` — prefer `loopy`.

## Initialize the Target Repo

`cd` into the target repo, then:

```bash
loopy init
# or
loopy init --template inner_outer_eval
```

Idempotent. Creates:

- `loopy_loop_config.yaml` — root config, edit this
- `.loopy_loop/workflows/goal_check/{prompt.txt,config.yaml}` — reserved workflow
- `.gitignore` entries for `.loopy_loop/sessions/` and `.loopy_loop/state.json*`

Use `--template inner_outer_eval` to scaffold the packaged outer/inner/eval
workflow set named `inner_outer_eval` instead of the default `goal_check`
workflow.

`goal_check` is reserved. Don't rename or delete it — it runs from iteration 1
onward and writes the authoritative `goal_check.json` that decides whether the
loop has met its goal. Advanced workflows can also emit `goal_check.json` by
setting `emits_goal_check: true`; keep the reserved `goal_check` workflow unless
you intentionally replace its role with another completion-signal workflow.

## Configure

### Root config — `loopy_loop_config.yaml`

```yaml
goal: "Ship a minimal working landing page"
completion_criteria:
  - "Homepage renders without errors"
stop_criteria:
  - "A workflow writes an unresolvable error flag"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "openai_compat"   # or "codex", "claude", "gemini"
team_harness_model: "gpt-5.4"
team_harness_agents: ["codex"]
team_harness_agent_models:
  codex: "gpt-5.4"
team_harness_agent_reasoning_efforts: {}
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
team_harness_system_prompt_extension: ""
```

Constraints:

- `goal_hash` is derived from `goal` and used in session ids and session metadata.
- `team_harness_model` controls the coordinator. Use `team_harness_agent_models`
  to pin default worker subprocess models by agent type.
- `team_harness_api_base` is normalized: trailing slash stripped, `/v1` appended
  when missing — write whichever form you prefer.
- Unknown config keys are rejected. All `team_harness_*` field names are exact.
- The env var named in `team_harness_api_key_env` must be exported in the shell
  that starts the coordinator AND in the shell that starts each worker.
- Some providers (e.g. `codex`) skip the API-key check.

## Session State Files

Each session has:

- `project_state/` for workflow-owned durable markdown state
- `eval_checks/` for session-scoped eval-banana checks
- `updates_from_user.md` for user requests that arrive during a run
- `project_state/finished.md` for outer-verified completed work
- `harness_outputs/<NNNN>_<workflow_id>/<team_harness_run_id>/` for
  team-harness coordinator and worker artifacts

Outer workflows should read `updates_from_user.md` every run. If it contains
content, they should reflect it into `project_state/` first and clear the file
only after doing so. Inner workflows should not append final entries to
`finished.md`; the outer workflow owns verified completion tracking.

### Custom workflows — `.loopy_loop/workflows/<id>/`

Each workflow is a folder; the folder name is the workflow id.

```
.loopy_loop/workflows/<id>/
├── prompt.txt        # the prompt the workflow runs
└── config.yaml       # scheduling
```

`config.yaml`:

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

Rules:

- `must_follow` must resolve to an existing workflow during coordinator preflight.
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

- `emits_goal_check: true` tells the worker to include a `goal_check.json`
  output path in that workflow's prompt, and tells the coordinator to read it
  using the same stop logic as the reserved `goal_check` workflow.
- `goal_check` is reserved — pick a different id for new workflows.

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
description: "Run eval checks and stop when they pass."
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

## Session-Scoped Project State and Eval Checks

Every rendered workflow prompt includes these paths:

```text
Session directory: .loopy_loop/sessions/<session_id>
Session project_state directory: .loopy_loop/sessions/<session_id>/project_state
Session eval_checks directory: .loopy_loop/sessions/<session_id>/eval_checks
Iteration directory: .loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>
```

The runtime only provides the paths; it does not parse markdown state. Put the
ownership rules in each workflow prompt. A common reusable pattern is:

- `outer` owns high-level planning, status transitions, and
  `project_state/what_we_should_do/plan.md`
- `inner` implements exactly one available leaf task and marks it waiting for
  outer review
- `eval_reviewer` writes high-level eval-banana YAML checks under the session
  `eval_checks/`
- `eval_runner` runs only the session checks and writes `goal_check.json`

Useful `project_state/` files:

```text
project_state/
├── README.md
├── what_we_are_building.md
├── what_we_have.md
├── current_state.md
├── decisions.md
├── eval_results.md
└── what_we_should_do/
    ├── plan.md
    └── tasks/<task-id>/README.md
```

Session eval checks work well with eval-banana's explicit directory option:

```bash
eval-banana run --check-dir .loopy_loop/sessions/<session_id>/eval_checks
```

## Run

Two processes; start in separate terminals.

```bash
# terminal 1 — coordinator
export OPENROUTER_API_KEY=...            # whatever team_harness_api_key_env names
loopy coordinator --host 127.0.0.1 --port 8080
```

```bash
# terminal 2 — worker (multiple workers may share one coordinator)
export OPENROUTER_API_KEY=...
loopy worker --coordinator http://127.0.0.1:8080
```

The worker calls `/register` once for its first task, then loops on `/finished`
until it receives `action=stop`.

### Fresh start vs resume

On startup, the coordinator inspects `.loopy_loop/state.json`:

- Terminal state → archived to `state.json.archive_<timestamp>.json`, fresh start.
- Already `running` → startup fails unless `--resume` is passed.

```bash
loopy coordinator --resume
```

Use `--resume` when reattaching to a live state file (coordinator was killed
without setting a terminal state).

## Monitor and Stop

```bash
loopy status   # session id, iteration count, current task, stop reason
loopy stop     # sets stop_requested=true under the file lock; workers exit after
               # their next /finished
```

Per-iteration artifacts live at:

```
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/
```

Authoritative control files live only inside the current iteration directory:

- `control.json` — `{"unresolvable_error": true, "reason": "...", "schema_version": 1}`
- `goal_check.json` (inside `*_goal_check` iterations or workflows configured
  with `emits_goal_check: true`) —
  `{"goal_met": false, "reason": "...", "schema_version": 1}`

If `goal_check.json` is repeatedly missing or invalid, the coordinator stops
with `stop_reason="goal_check_broken"` after `goal_check_consecutive_failures_cap`.

## Common Pitfalls

- **API key not exported** → coordinator preflight fails. Export the env var
  named in `team_harness_api_key_env` in both the coordinator and worker shells.
- **Unknown config field** → parser rejects it. `team_harness_*` field names
  are exact; check spelling.
- **Workflow id collision with `goal_check`** → reserved; pick a different id.
- **`must_follow` references a missing workflow** → preflight fails. The id
  must match a folder under `.loopy_loop/workflows/`.
- **`run_after_successes.workflow_id` references a missing workflow** →
  preflight fails. The id must also match a workflow folder.
- **Eval runner does not stop the loop** → confirm the workflow has
  `emits_goal_check: true` and writes valid JSON to the exact
  `goal_check.json output path` in its prompt.
- **Workflow ignores project state** → loopy-loop only injects state paths.
  The workflow prompt must explicitly say which `project_state/` files to read
  and update.
- **Re-running `loopy coordinator` against a still-running state file** →
  intentionally fatal. Pass `--resume` to attach.
- **Killing only the coordinator** → state stays `running`. Either pass
  `--resume` next time or `loopy stop` first to reach a terminal state.

## Reference

- HTTP contract: `docs/http-contract.md`
- Session layout: `docs/session-layout.md`
- Source: https://github.com/writeitai/loopy-loop
