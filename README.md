# loopy-loop

`loopy-loop` is a repo-local automation loop for AI agents.

- A FastAPI coordinator owns loop state in `.loopy_loop/state.json`.
- One or more blocking workers poll the coordinator over HTTP.
- Each assignment loads workflow files from disk, runs `team_harness.Harness`, writes iteration artifacts under `.loopy_loop/sessions/<session_id>/iterations/`, and reports completion back to the coordinator.

## Install

```bash
uv sync --extra dev
```

Or:

```bash
uv pip install .
```

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

Root config:

```yaml
goal: "Ship a minimal working landing page"
goal_slug: "ship-landing-page"
completion_criteria:
  - "Homepage renders without errors"
  - "Primary CTA is wired"
stop_criteria:
  - "A workflow writes an unresolvable error flag"
max_turns: 20
goal_check_consecutive_failures_cap: 3
model: "gpt-5.4"
agents: ["codex"]
api_base: "https://openrouter.ai/api/v1"
api_key_env: "OPENROUTER_API_KEY"
system_prompt_extension: ""
```

Rules:

- `goal_slug` must match `^[a-z0-9][a-z0-9_-]{0,63}$`
- `api_base` is normalized by loopy-loop: trailing slash stripped, `/v1` appended when missing
- `api_key_env` must be set during coordinator preflight and again in the worker before `Harness(...)`

Workflow config:

```yaml
enabled: true
run_every: 1
must_follow: null
not_before_iteration: 0
description: ""
```

Rules:

- Workflow id is the folder name under `.loopy_loop/workflows/`
- `must_follow` must resolve during coordinator preflight
- `run_every` is based on completed iteration count, not wall clock
- `goal_check` is reserved and scaffolded with `not_before_iteration: 1`

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

Every `Harness.run()` call is fresh. Continuity comes from:

- git state in the target repo
- `.loopy_loop/sessions/<session_id>/...` artifacts
- the coordinator state in `.loopy_loop/state.json`

`team-harness` may also emit its own native artifact tree relative to the repo root. loopy-loop keeps its own state and iteration artifacts separate under `.loopy_loop/`.

## Control Files

`control.json` is read only from the current iteration directory:

```json
{"unresolvable_error": true, "reason": "Missing credentials", "schema_version": 1}
```

`goal_check.json` is authoritative only at:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_goal_check/goal_check.json
```

```json
{"goal_met": false, "reason": "docs still missing", "schema_version": 1}
```

If `goal_check.json` is missing or invalid repeatedly, the coordinator stops with `stop_reason="goal_check_broken"` after the configured failure cap.
