---
name: loopy-loop
description: Set up and run loopy-loop, a repo-local automation loop that drives AI agents toward a goal across many iterations via a FastAPI coordinator and one or more workers. Use this skill when the user wants to install loopy-loop in a target repo, scaffold its config, define workflows, or operate the coordinator/worker pair (start, monitor, stop, resume).
---

# loopy-loop

`loopy-loop` runs an AI-agent improvement loop inside a target repository. Each
iteration runs one workflow via `team-harness`. A FastAPI coordinator owns loop
state in `.loopy_loop/state.json`; one or more blocking workers poll it over
HTTP and execute the assigned workflow.

Use this skill when the user asks to:

- Install loopy-loop or scaffold it in a repo (`loopy init`)
- Configure a goal, completion criteria, model, or workflow
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
```

Idempotent. Creates:

- `loopy_loop_config.yaml` — root config, edit this
- `.loopy_loop/workflows/goal_check/{prompt.txt,config.yaml}` — reserved workflow
- `.gitignore` entries for `.loopy_loop/sessions/` and `.loopy_loop/state.json*`

`goal_check` is reserved. Don't rename or delete it — it runs from iteration 1
onward and writes the authoritative `goal_check.json` that decides whether the
loop has met its goal.

## Configure

### Root config — `loopy_loop_config.yaml`

```yaml
goal: "Ship a minimal working landing page"
goal_slug: "ship-landing-page"           # ^[a-z0-9][a-z0-9_-]{0,63}$
completion_criteria:
  - "Homepage renders without errors"
stop_criteria:
  - "A workflow writes an unresolvable error flag"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "openai_compat"   # or "codex", "claude", "gemini"
team_harness_model: "gpt-5.4"
team_harness_agents: ["codex"]
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
team_harness_system_prompt_extension: ""
```

Constraints:

- `goal_slug` is part of session ids; the regex above is enforced.
- `team_harness_api_base` is normalized: trailing slash stripped, `/v1` appended
  when missing — write whichever form you prefer.
- Unknown config keys are rejected. All `team_harness_*` field names are exact.
- The env var named in `team_harness_api_key_env` must be exported in the shell
  that starts the coordinator AND in the shell that starts each worker.
- Some providers (e.g. `codex`) skip the API-key check.

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
run_every: 1               # run every N completed iterations
must_follow: null          # workflow id that must immediately precede this one
not_before_iteration: 0
description: ""
```

Rules:

- `must_follow` must resolve to an existing workflow during coordinator preflight.
- `run_every` counts completed iterations, not wall-clock time.
- `goal_check` is reserved — pick a different id for new workflows.

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
- `goal_check.json` (only inside `*_goal_check` iterations) —
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
- **Re-running `loopy coordinator` against a still-running state file** →
  intentionally fatal. Pass `--resume` to attach.
- **Killing only the coordinator** → state stays `running`. Either pass
  `--resume` next time or `loopy stop` first to reach a terminal state.

## Reference

- HTTP contract: `docs/http-contract.md`
- Session layout: `docs/session-layout.md`
- Source: https://github.com/writeitai/loopy-loop
