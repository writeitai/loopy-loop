# HTTP Contract

loopy-loop exposes exactly two coordinator endpoints.

## POST /register

Request: `{}` (empty body), or with the worker's process identity:

```json
{
  "worker": {
    "hostname": "buildbox",
    "pid": 4242,
    "starttime": "lstart:Sun Jul 12 00:00:00 2026"
  }
}
```

`worker` is optional and backward compatible. When present, the coordinator
stamps it onto the dispatched task so a later `/register` can *verify* whether
that worker is still alive before reclaiming its task. `starttime` is
team-harness's pid-reuse-proof process-identity token (requires team-harness
with process identity support; omitted otherwise).

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
    "team_harness_system_prompt_extension": "",
    "recovery_policy": "drain",
    "recovery_drain_timeout_s": 600.0
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
  `/finished`), `/register` proceeds in three steps:
  1. **Liveness check.** If the recorded worker identity is *verifiably still
     alive* (same host, matching pid + starttime), the call is refused with
     **HTTP 409** and no state is mutated — the task is not abandoned and no
     duplicate work is dispatched. Unverifiable identities (no identity
     recorded, remote host, no starttime token) fall through.
  2. **Result recovery.** The coordinator checks the current iteration
     directory for `pending_finished_request.json` or `result.json`. If either
     file proves the task completed, the completed task is recorded in history
     before checking stop conditions.
  3. **Orphan recovery.** With nothing recoverable, the coordinator applies the
     configured recovery policy (`recovery_policy`, default `drain` bounded by
     `recovery_drain_timeout_s`) to any agent processes the dead worker's
     harness run left behind, writes a `salvage.json` into the interrupted
     iteration directory when something was handled, and records the iteration
     as failed with `error="abandoned_after_drain"` (or plain `"abandoned"`
     when nothing was reaped). Requires team-harness with the process reaper;
     older versions skip this step. If team-harness's own guard reports the
     run's owning process still alive, `/register` returns **HTTP 409**.
  Note: draining can block `/register` for up to `recovery_drain_timeout_s`;
  the bundled worker uses an unbounded read timeout for this reason.
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
  "error": null,
  "worker": {
    "hostname": "buildbox",
    "pid": 4242,
    "starttime": "lstart:Sun Jul 12 00:00:00 2026"
  }
}
```

`worker` is optional (same semantics as `/register`): the calling worker will
run the next dispatched task, so its identity is stamped onto that task.

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
