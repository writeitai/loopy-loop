# HTTP Contract

loopy-loop v1 exposes exactly three coordinator endpoints.

## `POST /workers/register`

Request:

```json
{}
```

Response:

```json
{"worker_id": "worker_ab12cd34"}
```

## `POST /workers/{worker_id}/next`

Request:

```json
{}
```

Run response:

```json
{
  "action": "run",
  "stop_reason": null,
  "assignment_id": "b548df07-993e-4cfd-975a-3c9d40a0f770",
  "workflow_id": "planner",
  "session_id": "ship-landing-page_20260419_143022_ab12cd34",
  "iteration": 3,
  "config_snapshot": {
    "goal": "Ship a minimal working landing page",
    "goal_slug": "ship-landing-page",
    "completion_criteria": ["Homepage renders without errors"],
    "stop_criteria": ["A workflow writes an unresolvable error flag"],
    "max_turns": 20,
    "goal_check_consecutive_failures_cap": 3,
    "model": "gpt-5.4",
    "agents": ["codex"],
    "api_base": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
    "system_prompt_extension": ""
  }
}
```

Wait response:

```json
{
  "action": "wait",
  "stop_reason": null,
  "assignment_id": null,
  "workflow_id": null,
  "session_id": null,
  "iteration": null,
  "config_snapshot": null
}
```

Stop response:

```json
{
  "action": "stop",
  "stop_reason": "goal_met",
  "assignment_id": null,
  "workflow_id": null,
  "session_id": null,
  "iteration": null,
  "config_snapshot": null
}
```

Rules:

- If a worker already owns the active live lease, repeated `/next` returns the same `run` payload.
- If another worker owns the live lease, `/next` returns `wait`.
- If the lease is stale, `/next` records a `lease_expired` history entry, clears the assignment, and dispatches fresh work if an eligible workflow exists.

## `POST /workers/{worker_id}/finished`

Request:

```json
{
  "assignment_id": "b548df07-993e-4cfd-975a-3c9d40a0f770",
  "session_id": "ship-landing-page_20260419_143022_ab12cd34",
  "workflow_id": "planner",
  "success": true,
  "text": "done",
  "error": null
}
```

Response:

- Same response shape as `/next`
- Can immediately return `run`, `wait`, or `stop`

Rules:

- `/finished` is idempotent by `assignment_id`
- Unknown or stale `assignment_id` returns HTTP 200 with the current action and does not mutate state
- The coordinator reads `control.json` only from the current iteration directory
- The coordinator reads `goal_check.json` only from the current `goal_check` iteration directory

## Assignment Semantics

- `assignment_id` identifies one leased assignment
- A duplicate or late `/finished` for an expired lease is ignored safely
- The worker may retry `/finished` after transient HTTP failures; duplicates remain safe
