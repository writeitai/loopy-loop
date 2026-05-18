# HTTP Contract

loopy-loop exposes exactly two coordinator endpoints.

## POST /register

Request: `{}` (empty body)

Run response:

```json
{
  "action": "run",
  "workflow_set": "main",
  "workflow_id": "planner",
  "session_id": "20260419_143022_71393ee22450_ab12cd34",
  "iteration": 3,
  "config_snapshot": {
    "goal": "Ship a minimal working landing page",
    "goal_hash": "71393ee22450",
    "workflow_set": "main",
    "completion_criteria": ["Homepage renders without errors"],
    "stop_criteria": ["A workflow updates session control.json to stopped"],
    "max_turns": 20,
    "goal_check_consecutive_failures_cap": 3,
    "team_harness_provider": "openai_compat",
    "team_harness_model": "gpt-5.5",
    "team_harness_agents": ["codex"],
    "team_harness_agent_models": {"codex": "gpt-5.5"},
    "team_harness_agent_reasoning_efforts": {"codex": "high"},
    "team_harness_max_retries": null,
    "team_harness_retry_base_delay_s": null,
    "team_harness_retry_max_delay_s": null,
    "team_harness_api_base": "https://openrouter.ai/api/v1",
    "team_harness_api_key_env": "OPENROUTER_API_KEY",
    "team_harness_system_prompt_extension": ""
  },
  "stop_reason": null
}
```

Stop response:

```json
{
  "action": "stop",
  "stop_reason": "goal_met",
  "workflow_set": null,
  "workflow_id": null,
  "session_id": null,
  "iteration": null,
  "config_snapshot": null
}
```

Rules:

- `config_snapshot.goal` is the resolved goal text loaded from
  `loopy_loop_config.yaml`'s `goal_file`; workers and team-harness never receive
  the goal file path as the goal.
- `workflow_set` tells the worker which
  `.loopy_loop/workflow_sets/<workflow_set>/workflows/<workflow_id>/` directory
  to load.
- If `current_task` is already set (previous worker crashed without calling
  `/finished`), `/register` first checks the current iteration directory for
  `pending_finished_request.json` or `result.json`. If either file proves the
  task completed, the coordinator records the completed task in history before
  checking stop conditions. Only tasks with no recoverable local completion are
  recorded as failed with `error="abandoned"`.
- If the loop is in a terminal state, `/register` immediately returns `action=stop`.

## POST /finished

Request:

```json
{
  "workflow_id": "planner",
  "session_id": "20260419_143022_71393ee22450_ab12cd34",
  "iteration": 3,
  "success": true,
  "text": "done",
  "error": null
}
```

Response: same shape as `/register` response (`action` is either `"run"` or `"stop"`).

Rules:

- If `session_id` + `workflow_id` + `iteration` does not match `current_task`,
  the call is treated as stale: state is not mutated, `current_task` is not
  changed, and the current task's run response is returned to the caller.
- If `current_task` is `None` (no task is active), the coordinator dispatches the next
  available task as if `/register` had been called. If the state is terminal, it returns
  `action=stop`.
- The coordinator reads `control.json` only from the session directory.
- The coordinator reads `goal_check.json` only from the current iteration directory
  when the workflow is `goal_check` or has `emits_goal_check=true`.
- A valid `goal_check.json` is an eval artifact, not a stop switch. Workflows
  stop the loop by updating session `control.json`.
- A parent workflow can request a depth-first child loop by writing a
  `schema_version: 1` JSON request with `workflow_set` and `goal` under the
  active session's `child_requests/` directory. The next response dispatches the
  child workflow set; after the child session stops, the coordinator resumes the
  parent session.
