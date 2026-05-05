# loopy-loop

`loopy-loop` is a repo-local automation loop for AI agents.

- A FastAPI coordinator owns loop state in `.loopy_loop/state.json`.
- One or more blocking workers poll the coordinator over HTTP.
- Each assignment loads workflow files from disk, runs `team_harness.TeamHarness`, writes iteration artifacts under `.loopy_loop/sessions/<session_id>/iterations/`, and reports completion back to the coordinator.

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
    ├── workflows/<workflow_id>/{prompt.txt,config.yaml}
    ├── sessions/<session_id>/...
    ├── state.json
    └── state.json.lock
```

## Quick Start

```bash
loopy init
loopy coordinator --host 127.0.0.1 --port 8080
loopy worker --coordinator http://127.0.0.1:8080
loopy status
loopy stop
```

`loopy init` is idempotent. It creates:

- `loopy_loop_config.yaml`
- `.loopy_loop/workflows/goal_check/prompt.txt`
- `.loopy_loop/workflows/goal_check/config.yaml`
- `.gitignore` entries for `.loopy_loop/sessions/` and `.loopy_loop/state.json*`

## CLI Reference

`loopy init`

- Scaffolds the root config and reserved `goal_check` workflow.
- Does not overwrite existing workflow files.

`loopy coordinator --host 0.0.0.0 --port 8080 [--resume]`

- Runs the FastAPI coordinator with exactly two endpoints: `/register` and `/finished`.
- On a fresh start, creates a new session directory and state file.
- If `.loopy_loop/state.json` is terminal, archives it to `.loopy_loop/state.json.archive_<timestamp>.json` and starts fresh.
- If `.loopy_loop/state.json` is already `running`, startup fails unless `--resume` is passed.

`loopy worker --coordinator http://127.0.0.1:8080`

- Calls `/register` once to receive the first task.
- Loops calling `/finished` after each completed task until it receives a `stop` response.
- Loads `loopy_loop_config.yaml`, `.loopy_loop/workflows/<workflow_id>/config.yaml`, and `.loopy_loop/workflows/<workflow_id>/prompt.txt` from disk on each task.
- Uses the coordinator `config_snapshot` as the execution snapshot for the session.

`loopy status`

- Prints current session id, iteration count, current task, and stop reason.

`loopy stop`

- Sets `stop_requested=true` in `.loopy_loop/state.json` under the repo-local file lock.

## Config Reference

Root config (`loopy_loop_config.yaml` at the repo root):

```yaml
goal_file: "loopy_loop_goal.txt"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "openai_compat"
team_harness_model: "gpt-5.4"
team_harness_agents: ["codex"]
team_harness_agent_models:
  codex: "gpt-5.4"
team_harness_agent_reasoning_efforts:
  codex: "high"
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
```

Rules:

- Session ids and session metadata include a deterministic `goal_hash` derived
  from the text loaded from `goal_file`
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

- Workflow id is the folder name under `.loopy_loop/workflows/`
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
  and participate in the same stop logic as the reserved `goal_check` workflow
- `goal_check` is reserved and scaffolded with `not_before_iteration: 1`

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

Stale `/finished` calls (mismatched `session_id` or `workflow_id`) do not mutate state and
return the current running task's response. If there is no active task, `/finished` acts
like `/register` and dispatches the next available task.

See [docs/http-contract.md](docs/http-contract.md) for the exact JSON payloads.

## Session Continuity

Every `TeamHarness.run()` call is fresh. Continuity comes from:

- git state in the target repo
- `.loopy_loop/sessions/<session_id>/...` artifacts
- the coordinator state in `.loopy_loop/state.json`

`team-harness` outputs are routed into the active loopy-loop session under
`.loopy_loop/sessions/<session_id>/harness_outputs/<NNNN>_<workflow_id>/<team_harness_run_id>/`.

Workflow prompts receive session-scoped paths for reusable project state and
eval definitions:

- `.loopy_loop/sessions/<session_id>/project_state/`
- `.loopy_loop/sessions/<session_id>/eval_checks/`
- `.loopy_loop/sessions/<session_id>/updates_from_user.md`
- `.loopy_loop/sessions/<session_id>/project_state/finished.md`
- `.loopy_loop/sessions/<session_id>/harness_outputs/`

These directories are workflow-owned. The coordinator only owns
`.loopy_loop/state.json` and iteration dispatch state.

Write runtime requests for the outer loop into `updates_from_user.md`. The outer
workflow should treat non-empty content as highest-priority planning input,
reflect it into `project_state/`, and then clear the file. Verified completed
work belongs in `project_state/finished.md`; `what_we_have.md` should remain the
concise current capability summary.

## Control Files

`control.json` is read only from the current iteration directory:

```json
{"unresolvable_error": true, "reason": "Missing credentials", "schema_version": 1}
```

`goal_check.json` is authoritative only at the current iteration directory for
the reserved `goal_check` workflow or any workflow with `emits_goal_check=true`:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/goal_check.json
```

```json
{"goal_met": false, "reason": "docs still missing", "schema_version": 1}
```

If `goal_check.json` is missing or invalid repeatedly, the coordinator stops with `stop_reason="goal_check_broken"` after the configured failure cap.
