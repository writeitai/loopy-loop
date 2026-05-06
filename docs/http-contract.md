# HTTP Contract

loopy-loop exposes exactly two coordinator endpoints.

## POST /register

Request: `{}` (empty body)

Run response:

```json
{
  "action": "run",
  "workflow_id": "planner",
  "session_id": "71393ee22450_20260419_143022_ab12cd34",
  "iteration": 3,
  "config_snapshot": {
    "goal": "Ship a minimal working landing page",
    "goal_hash": "71393ee22450",
    "completion_criteria": ["Homepage renders without errors"],
    "stop_criteria": ["A workflow writes an unresolvable error flag"],
    "max_turns": 20,
    "goal_check_consecutive_failures_cap": 3,
    "team_harness_provider": "openai_compat",
    "team_harness_model": "gpt-5.5",
    "team_harness_agents": ["codex"],
    "team_harness_agent_models": {"codex": "gpt-5.5"},
    "team_harness_agent_reasoning_efforts": {"codex": "high"},
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
- If `current_task` is already set (previous worker crashed without calling `/finished`),
  `/register` records it as failed (`error="abandoned"`) in history and then dispatches
  fresh work. Abandoned cleanup always runs before stop-condition evaluation.
- If the loop is in a terminal state, `/register` immediately returns `action=stop`.

## POST /finished

Request:

```json
{
  "workflow_id": "planner",
  "session_id": "71393ee22450_20260419_143022_ab12cd34",
  "success": true,
  "text": "done",
  "error": null
}
```

Response: same shape as `/register` response (`action` is either `"run"` or `"stop"`).

Rules:

- If `session_id` + `workflow_id` does not match `current_task`, the call is treated as
  stale: state is not mutated, `current_task` is not changed, and the current task's run
  response is returned to the caller.
- If `current_task` is `None` (no task is active), the coordinator dispatches the next
  available task as if `/register` had been called. If the state is terminal, it returns
  `action=stop`.
- The coordinator reads `control.json` only from the current iteration directory.
- The coordinator reads `goal_check.json` only from the current iteration directory
  when the workflow is `goal_check` or has `emits_goal_check=true`.
