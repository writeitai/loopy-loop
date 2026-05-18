# loopy-loop

`loopy-loop` is a repo-local automation loop for AI agents.

- A FastAPI coordinator owns session-local loop state in
  `.loopy_loop/sessions/<session_id>/state.json`.
- One or more blocking workers poll the coordinator over HTTP.
- Each assignment loads workflow files from the active workflow set, runs
  `team_harness.TeamHarness`, writes iteration artifacts under
  `.loopy_loop/sessions/<session_id>/iterations/`, and reports completion back
  to the coordinator.

## Install

```bash
uv sync --extra dev
```

Or:

```bash
uv pip install .
```

## Skills

This repo ships an [Agent Skill](https://support.claude.com/en/articles/12512176-what-are-skills)
that teaches Claude Code, Codex, and other compatible agents how to set up and
run loopy-loop in a target repo. Install it with:

```bash
npx skills add https://github.com/writeitai/loopy-loop --skill loopy-loop
```

The skill source lives under [`skills/loopy-loop/`](./skills/loopy-loop/SKILL.md).

## Repo Layout

```text
target repo/
├── loopy_loop_config.yaml
└── .loopy_loop/
    ├── workflow_sets/<workflow_set>/workflows/<workflow_id>/{prompt.txt,config.yaml}
    └── sessions/<session_id>/
        ├── goal.md
        ├── state.json
        ├── child_requests/
        ├── children/
        └── iterations/
```

## Quick Start

```bash
loopy init
loopy init --template inner_outer_eval
loopy coordinator --host 127.0.0.1 --port 8080
loopy worker --coordinator http://127.0.0.1:8080
loopy status
loopy stop
```

`loopy init` is idempotent. It creates:

- `loopy_loop_config.yaml`
- `.loopy_loop/workflow_sets/main/workflows/goal_check/prompt.txt`
- `.loopy_loop/workflow_sets/main/workflows/goal_check/config.yaml`
- `.gitignore` entry for `.loopy_loop/sessions/`

Use `loopy init --template inner_outer_eval` to scaffold the packaged workflow
set for outer planning, inner implementation, eval review, and eval running.

## CLI Reference

`loopy init [--template default|inner_outer_eval]`

- Scaffolds the root config and reserved `goal_check` workflow.
- `--template inner_outer_eval` scaffolds the packaged inner/outer/eval workflow
  set instead: `outer`, `inner`, `eval_reviewer`, and `eval_runner`.
- Does not overwrite existing workflow files.

`loopy coordinator --host 0.0.0.0 --port 8080 [--resume] [--workflow-set NAME] [--goal-file PATH]`

- Runs the FastAPI coordinator with exactly two endpoints: `/register` and `/finished`.
- On a fresh start, creates a new session directory with `goal.md` and
  session-local `state.json`.
- If the latest session `state.json` is terminal, archives it next to that
  session state file and starts fresh.
- If the latest session `state.json` is already `running`, startup fails unless
  `--resume` is passed.
- `--workflow-set` overrides `workflow_set` for the new session.
- `--goal-file` overrides the configured goal file for the new session.

`loopy worker --coordinator http://127.0.0.1:8080`

- Calls `/register` once to receive the first task.
- Loops calling `/finished` after each completed task until it receives a `stop` response.
- Loads `loopy_loop_config.yaml`,
  `.loopy_loop/workflow_sets/<workflow_set>/workflows/<workflow_id>/config.yaml`,
  and the matching `prompt.txt` from disk on each task.
- Uses the coordinator `config_snapshot` as the execution snapshot for the session.

`loopy status`

- Prints current session id, iteration count, current task, and stop reason.

`loopy stop`

- Sets `stop_requested=true` in the latest session-local `state.json`.

## Config Reference

Root config (`loopy_loop_config.yaml` at the repo root):

```yaml
goal_file: "loopy_loop_goal.txt"
workflow_set: "main"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "openai_compat"
team_harness_model: "gpt-5.5"
team_harness_agents: ["codex"]
team_harness_agent_models:
  codex: "gpt-5.5"
team_harness_agent_reasoning_efforts:
  codex: "high"
# Optional coordinator retry controls. Omit to use team-harness defaults.
# team_harness_max_retries: 8
# team_harness_retry_base_delay_s: 2.0
# team_harness_retry_max_delay_s: 60.0
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
```

Rules:

- Session ids start with a UTC timestamp for filesystem sorting and include a
  deterministic `goal_hash` derived from the text loaded from `goal_file`
- `workflow_set` names the workflow set used when `loopy coordinator`
  is started without `--workflow-set`
- `goal_file` is resolved relative to `loopy_loop_config.yaml`; inline `goal`
  values in YAML are rejected
- `completion_criteria`, `stop_criteria`, and
  `team_harness_system_prompt_extension` are optional; omitted criteria default
  to empty lists and the prompt extension defaults to an empty string
- `team_harness_model` controls the team-harness coordinator model; worker
  subprocess defaults are controlled by `team_harness_agent_models`
- `team_harness_api_base` is normalized by loopy-loop: trailing slash stripped, `/v1` appended when missing
- `team_harness_api_key_env` must be set during coordinator preflight and again in the worker before `TeamHarness(...)`
- `team_harness_agent_reasoning_efforts` is optional and only affects workers
  whose team-harness template supports a reasoning-effort flag
- `team_harness_max_retries`, `team_harness_retry_base_delay_s`, and
  `team_harness_retry_max_delay_s` are optional coordinator retry controls for
  transient team-harness API/network errors

Workflow config:

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

- Workflow id is the folder name under
  `.loopy_loop/workflow_sets/<workflow_set>/workflows/`
- `must_follow` must resolve during coordinator preflight
- `run_every` is based on completed iteration count, not wall clock
- `priority` breaks ties among eligible workflows; higher values run first
- `run_on_start=true` makes a workflow eligible before any successful workflow has run
- `run_after_successes` can run a workflow after every N successful runs of another workflow:

```yaml
run_after_successes:
  workflow_id: inner
  every: 10
```

- `emits_goal_check=true` lets a non-`goal_check` workflow write `goal_check.json`
  as a required eval artifact. To stop the loop, the workflow must update the
  session-scoped `control.json`.
- `goal_check` is reserved and scaffolded with `not_before_iteration: 1`

## Workflow Sets and Child Sessions

Workflow sets are mandatory. Even a single-loop repo uses
`.loopy_loop/workflow_sets/main/workflows/...`; the old
`.loopy_loop/workflows/...` layout is not loaded.

A workflow can request one sequential child session by writing a JSON file under
the active session's `child_requests/` directory:

```json
{
  "workflow_set": "pm_planner_dispatcher",
  "goal": "Implement the selected planner item.",
  "schema_version": 1
}
```

The coordinator creates the child session under the parent session's
`children/` directory, copies the request goal into the child `goal.md`, runs
the requested workflow set, and resumes the parent session after the child
reaches a terminal state. v1 is depth-first and single-child-at-a-time.

Cadence example:

```yaml
# Run eval_reviewer at the beginning and then after every 10 successful inner runs.
enabled: true
priority: 100
run_on_start: true
run_after_successes:
  workflow_id: inner
  every: 10
```

## HTTP Contract Summary

Endpoints:

- `POST /register`
- `POST /finished`

Both endpoints return a `TaskResponse` with `action` of either `"run"` or `"stop"`.

A `run` response carries `workflow_id`, `session_id`, `iteration`, and `config_snapshot`.
A `stop` response carries `stop_reason`.

Stale `/finished` calls (mismatched `session_id`, `workflow_id`, or
`iteration`) do not mutate state and return the current running task's response.
If there is no active task, `/finished` acts like `/register` and dispatches the
next available task. If a worker exits after writing `result.json` but before
`/finished` is acknowledged, the next `/register` recovers the completed result
from the iteration directory instead of marking it `abandoned`.

See [docs/http-contract.md](docs/http-contract.md) for the exact JSON payloads.

## Session Continuity

Every `TeamHarness.run()` call is fresh. Continuity comes from:

- git state in the target repo
- `.loopy_loop/sessions/<session_id>/...` artifacts
- the coordinator state in `.loopy_loop/sessions/<session_id>/state.json`

`team-harness` outputs are routed into the active loopy-loop session under
`.loopy_loop/sessions/<session_id>/harness_outputs/<NNNN>_<workflow_id>/<team_harness_run_id>/`.

Workflow prompts receive session-scoped paths for reusable project state and
eval definitions:

- `.loopy_loop/sessions/<session_id>/project_state/`
- `.loopy_loop/sessions/<session_id>/eval_checks/`
- `.loopy_loop/sessions/<session_id>/goal.md`
- `.loopy_loop/sessions/<session_id>/updates_from_user.md`
- `.loopy_loop/sessions/<session_id>/child_requests/`
- `.loopy_loop/sessions/<session_id>/control.json`
- `.loopy_loop/sessions/<session_id>/project_state/finished.md`
- `.loopy_loop/sessions/<session_id>/harness_outputs/`

These directories are workflow-owned. The coordinator only owns
`.loopy_loop/sessions/<session_id>/state.json` and iteration dispatch state.

Each session's `goal.md` is the source of truth for the target, constraints,
and completion intent. For top-level sessions it is copied from `goal_file` or
the `--goal-file` CLI override. Workflow state should not copy or restate that
goal.
`project_state/README.md` should explain state ownership: `memory.md` is
essential durable facts only, `finished.md` is outer-owned accepted completions
only, `eval_results.md` owns eval detail, and `current_state.md` carries live
status, the latest eval headline, and the next action.

Write runtime requests for the outer loop into `updates_from_user.md`. The outer
workflow should treat non-empty content as highest-priority planning input,
reflect it into `project_state/`, and then clear the file. Verified completed
work belongs in `project_state/finished.md`; `what_we_have.md` should remain the
concise current capability summary.

Eval workflows should run session checks with `--output-dir` pointing at
`.loopy_loop/sessions/<session_id>/eval_results/`. Raw eval-banana reports stay
there; `project_state/eval_results.md` should summarize and link the latest
reports instead of copying them. `project_state/current_state.md` should only
carry the latest eval headline and the next action.

For implementation work that changes repo files, the default delivery path is a
branch, PR, passing checks, and merge. Multi-repo work should create and merge
one PR per changed repo when possible. Tasks should opt out only when they are
session-state-only, eval-only, research-only, planning-only, or the repo has no
usable remote or auth. `project_state/finished.md` should record delivery
evidence for each changed repo: repo, branch, PR URL, merge status, merge
commit, and checks/CI status. If PR creation or merge is blocked, record the
exact blocker and remaining action in `project_state/current_state.md`.

## Control Files

`control.json` is the session-scoped workflow stop switch:

```json
{
  "state": "running",
  "reason": "session active",
  "stop_reason": null,
  "schema_version": 1
}
```

Workflows leave it alone while the loop should continue. To stop successfully:

```json
{
  "state": "stopped",
  "reason": "evals passed",
  "stop_reason": "goal_met",
  "schema_version": 1
}
```

To stop because the loop cannot continue:

```json
{
  "state": "stopped",
  "reason": "specific terminal blocker",
  "stop_reason": "unresolvable_error",
  "schema_version": 1
}
```

`goal_check.json` is a per-iteration eval artifact for the reserved
`goal_check` workflow or any workflow with `emits_goal_check=true`:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/goal_check.json
```

```json
{"goal_met": false, "reason": "docs still missing", "schema_version": 1}
```

If `goal_check.json` is missing or invalid repeatedly, the coordinator stops
with `stop_reason="goal_check_broken"` after the configured failure cap. A
valid `goal_check.json` does not stop the loop by itself; stopping is controlled
by session `control.json`.
