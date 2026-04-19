# Plan: Simplify API — Drop Leases, Polling, and Worker Identity

## Overview

The current loopy-loop coordinator supports multiple concurrent workers by issuing leased assignments and making idle workers poll `/next` until they receive `run`, `wait`, or `stop`. In practice, the system runs one worker at a time, making the lease, wait-action, and persistent worker-identity machinery pure overhead. This refactor removes all of that: `/register` and `/finished` both return a `TaskResponse` directly, so the worker loop becomes a simple ping-pong — register once, execute, report finished, repeat. There is no polling, no `worker_id` in URLs, no `WorkerState`, no `ActiveAssignment.lease_seconds`, and no `assignment_id` tracking for idempotency. The scheduler, goal-check logic, control signals, history, stop reasons, and file lock are all kept unchanged.

---

## Design Decisions

**1. `TaskResponse` instead of two separate response shapes.**
Register and finished both return the same model. This eliminates `RegisterWorkerResponse` and `NextActionResponse` and makes the client trivial to write.

**2. Drop `assignment_id` entirely.**
`assignment_id` existed to make `/finished` idempotent across retries in the presence of leases. Without leases, the stale-call problem is solved differently: `/finished` matches by `session_id` + `workflow_id` against `current_task`. A mismatch (stale call) returns the current action without mutating state, which is safe.

**3. Crash recovery via `/register`.**
If the worker crashes mid-run and a new worker calls `/register`, the coordinator sees an orphaned `current_task` (still set, no `/finished` ever arrived). It records that task as failed with `error="abandoned"` in history, increments `iteration_count`, clears `current_task`, and then dispatches fresh work. This replaces lease-expiry reclaim with a simpler crash-recovery mechanism that fires naturally on the next worker startup.

**4. Keep `current_task` instead of `active_assignment`.**
`CurrentTask` is a leaner version: no `worker_id`, no `lease_seconds`, no `assignment_id`. It only needs `workflow_id`, `session_id`, `iteration`, and `started_at` (for auditing/history).

**5. `HistoryEntry` drops `assignment_id` and `worker_id`.**
Both fields are gone from the system. `session_id` already identifies the session; `iteration` identifies the slot. The history record is still complete enough to audit what ran and what happened.

**6. No retry loop in the worker.**
The old worker retried `/finished` up to N times with backoff because a transient HTTP error could leave the coordinator stuck with a live lease. Now, if `/finished` fails transiently, the worker can just retry the same call — it is safe because the coordinator ignores stale calls. A simple `httpx` retry with one or two attempts and a short backoff is sufficient. If all retries fail, the worker exits; the next invocation will call `/register`, which recovers via the abandoned-task path.

**7. `DEFAULT_LOCK_TIMEOUT_SECONDS` stays in `models.py` (used by `StateStore`).**
All other `DEFAULT_*` constants are removed.

**8. `status` command shows `current_task` instead of `active_assignment`.**
The output changes slightly (`worker` field disappears) but the intent is the same.

---

## Files to Create / Modify / Delete

### Modify

- `src/loopy_loop/models.py` — remove dead constants and models; add `CurrentTask`, `TaskResponse`; update `FinishedRequest`, `LoopState`, `HistoryEntry`
- `src/loopy_loop/coordinator_app.py` — new endpoints `/register` and `/finished`; rewrite `CoordinatorService`; remove `_reclaim_expired_assignment`, `_require_worker`, `_dispatch_next_action` (replace with simpler helpers)
- `src/loopy_loop/worker.py` — simplify loop; remove polling, retry loop, `_post_next`, `_register_worker` returning worker_id; remove dead imports
- `src/loopy_loop/cli.py` — update `status` command to use `current_task`; update CLI docstring for `coordinator` (three endpoints → two); update `worker` docstring
- `src/tests/conftest.py` — update `state_factory` and `history_entry_factory`; remove `assignment_factory`; add `current_task_factory`
- `src/tests/test_coordinator_app.py` — full rewrite against new endpoints
- `src/tests/test_worker.py` — full rewrite against new loop shape
- `src/tests/test_idempotent_finished.py` — rewrite to use new stale-detection logic
- `src/tests/test_fresh_run_archive.py` — minor update (no `workers` in state)
- `docs/http-contract.md` — full rewrite for new two-endpoint contract
- `README.md` — update HTTP Contract Summary and CLI Reference sections

### Delete

- `src/tests/test_lease_reclaim.py` — tests `_reclaim_expired_assignment`, which is removed

### No change needed

- `src/loopy_loop/config.py`
- `src/loopy_loop/scheduler.py`
- `src/loopy_loop/sessions.py`
- `src/loopy_loop/harness_runner.py`
- `src/loopy_loop/state_store.py` (only uses `DEFAULT_LOCK_TIMEOUT_SECONDS`, which stays in `models.py`)
- `src/tests/test_config.py`
- `src/tests/test_scheduler.py`
- `src/tests/test_sessions.py`
- `src/tests/test_state_store.py`
- `src/tests/test_api_base_normalization.py`
- `src/tests/test_cli.py` (minor: if it references `workers` in status output, update that assertion)
- `src/tests/test_goal_check_gate.py`
- `src/tests/test_must_follow_success.py`

---

## New Models

### `CurrentTask`

Replaces `ActiveAssignment`. Tracks what is currently executing.

```python
class CurrentTask(BaseModel):
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    iteration: int = Field(...)
    started_at: datetime = Field(...)
```

### `TaskResponse`

Returned by both `/register` and `/finished`.

```python
class TaskResponse(BaseModel):
    action: Literal["run", "stop"] = Field(...)
    workflow_id: str | None = Field(default=None)   # set when action == "run"
    session_id: str | None = Field(default=None)    # set when action == "run"
    iteration: int | None = Field(default=None)     # set when action == "run"
    config_snapshot: RootConfigSnapshot | None = Field(default=None)  # set when action == "run"
    stop_reason: str | None = Field(default=None)   # set when action == "stop"
```

### Updated `FinishedRequest`

Drops `assignment_id`. Keeps everything needed to match against `current_task` and record history.

```python
class FinishedRequest(BaseModel):
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    success: bool = Field(...)
    text: str | None = Field(default=None)
    error: str | None = Field(default=None)
```

### Updated `HistoryEntry`

Drops `assignment_id` and `worker_id`.

```python
class HistoryEntry(BaseModel):
    iteration: int = Field(...)
    workflow_id: str = Field(...)
    session_id: str = Field(...)
    success: bool = Field(...)
    error: str | None = Field(default=None)
    started_at: datetime = Field(...)
    finished_at: datetime = Field(...)
```

### Updated `LoopState`

Removes `workers` dict and `active_assignment`; adds `current_task`.

```python
class LoopState(BaseModel):
    status: Literal["running", "stopped", "goal_met", "failed", "max_turns"] = Field(
        default="running"
    )
    goal_slug: str = Field(...)
    max_turns: int = Field(...)
    active_session_id: str = Field(...)
    goal_met: bool = Field(default=False)
    stop_requested: bool = Field(default=False)
    unresolvable_error: bool = Field(default=False)
    stop_reason: str | None = Field(default=None)
    iteration_count: int = Field(default=0)
    goal_check_consecutive_failures: int = Field(default=0)
    current_task: CurrentTask | None = Field(default=None)
    history: list[HistoryEntry] = Field(default_factory=list)
    config_snapshot: RootConfigSnapshot = Field(...)
```

### Constants to remove from `models.py`

```python
# DELETE these:
DEFAULT_LEASE_SECONDS
DEFAULT_POLL_INTERVAL_SECONDS
DEFAULT_FINISHED_RETRY_ATTEMPTS
DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS
WAIT_ACTION

# DELETE these classes:
WorkerState
ActiveAssignment
RegisterWorkerResponse
NextActionResponse
```

### Constants to keep in `models.py`

```python
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0   # used by StateStore
CONTROL_SCHEMA_VERSION = 1
GOAL_CHECK_SCHEMA_VERSION = 1
RUN_ACTION = "run"
STOP_ACTION = "stop"
```

---

## Endpoint Behavior

### `POST /register`

No request body needed (send `{}`).

**Logic:**

1. Acquire file lock, read state.
2. If `state` is `None`: raise `RuntimeError` (coordinator not initialized — should never happen).
3. If `current_task` is set (crashed worker that never called `/finished`):
   - Record `current_task` as a failed `HistoryEntry` with `error="abandoned"`, `finished_at=now`.
   - Increment `iteration_count`.
   - Clear `current_task = None`.
4. Call `_stop_response_if_needed(state)`. If a stop condition is active, return `TaskResponse(action="stop", stop_reason=...)`.
5. Call `choose_next_workflow(...)`. If no eligible workflow, set `state.stop_reason = "no_eligible_workflow"`, `state.status = "failed"`, return `TaskResponse(action="stop", stop_reason="no_eligible_workflow")`.
6. Set `current_task = CurrentTask(workflow_id=workflow.id, session_id=state.active_session_id, iteration=state.iteration_count + 1, started_at=now)`.
7. Write state. Return `TaskResponse(action="run", workflow_id=..., session_id=..., iteration=..., config_snapshot=...)`.

**Edge cases:**
- If the loop is already in a terminal status when `/register` is called (e.g., another process stopped it between `/finished` returning `run` and this register call), step 4 returns stop immediately and `current_task` is never set. This is safe.

---

### `POST /finished`

Request body: `FinishedRequest`.

**Logic:**

1. Acquire file lock, read state.
2. If `state` is `None`: raise `RuntimeError`.
3. If `current_task` is `None`:
   - This is a stale call (coordinator has no active task). Return the current action without mutating state: check stop conditions and return `TaskResponse` accordingly (stop if terminal, or dispatch next — but since no task is active, just return the current terminal/next state). **Simplest approach:** return `_build_current_response(state)` which checks stop, and if not stopping, dispatches a new task. This is safe because if `current_task` is `None`, there is nothing to double-record.
   - Actually, the cleaner behavior: if `current_task` is `None`, treat as stale — do not mutate history, do not dispatch. Return the current state's stop response if terminal, otherwise return stop with reason `"no_current_task"`. This prevents accidentally triggering a new dispatch on a stale call.
   - **Decision:** Return `TaskResponse(action="stop", stop_reason="stale_request")` when `current_task` is None. This is the clearest signal to the caller. The worker should only call `/finished` once per task, so a stale call means something is wrong.

   Wait — there is a subtlety. The old behavior returned the "current next action" on stale `/finished` calls, so the worker could keep going. In the new design the worker calls `/finished` → gets next task → runs → calls `/finished`. If `/finished` returns `stale_request`, the worker does not know what to do next. Better: on stale call, return the current dispatch (same as calling `/register` fresh). This mirrors the old idempotency behavior and keeps the worker loop coherent.

   **Final decision for stale `/finished`:** If `current_task` is `None`, dispatch as if it were a fresh `/register`: check stop conditions, pick next workflow, set `current_task`, return `TaskResponse`. This means a stale `/finished` after a crash recovery is fully handled.

4. If `current_task` is set but `request.session_id != current_task.session_id` or `request.workflow_id != current_task.workflow_id`:
   - Mismatch: stale call for a different task. Do not mutate state.
   - Return `_run_response(current_task=current_task, snapshot=state.config_snapshot)`. The caller gets told to keep running the current task.
5. Match confirmed. Record result:
   a. `success = request.success`, `error = request.error`.
   b. If `_has_invalid_control_output(current_task)`: `success = False`, `error = "invalid_control_output"`.
   c. If `current_task.workflow_id == "goal_check"`: read `goal_check.json`. If missing/invalid: `success = False`, `error = "invalid_goal_check_output"`, increment `goal_check_consecutive_failures`. If cap reached: set `state.stop_reason = "goal_check_broken"`, `state.status = "failed"`. Else: reset `goal_check_consecutive_failures = 0`; if `goal_signal.goal_met`: set `state.goal_met = True`.
   d. If `_has_unresolvable_error_signal(current_task)`: `state.unresolvable_error = True`.
   e. If stop reason is not `goal_check_broken`: call `_apply_stop_precedence(state)`.
   f. Append `HistoryEntry(iteration=current_task.iteration, workflow_id=..., session_id=..., success=success, error=error, started_at=current_task.started_at, finished_at=now)`.
   g. `state.iteration_count += 1`.
   h. `state.current_task = None`.
6. If `state.stop_reason == "goal_check_broken"`: return `TaskResponse(action="stop", stop_reason="goal_check_broken")`.
7. Check stop conditions. If stopping, return `TaskResponse(action="stop", stop_reason=...)`.
8. Dispatch next: `choose_next_workflow(...)`. If no eligible workflow, set stop state, return stop response.
9. Set `current_task`, write state. Return `TaskResponse(action="run", ...)`.

---

## Worker Loop

```python
def run_worker_loop(*, repo_root: Path, coordinator_url: str) -> None:
    base_url = coordinator_url.rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        task = _post_register(client=client, coordinator_url=base_url)
        while task.action == "run":
            finished_request = _run_task(repo_root=repo_root, task=task)
            task = _post_finished(
                client=client,
                coordinator_url=base_url,
                request=finished_request,
            )
        # task.action == "stop"
        return
```

Notes:
- `_run_task` is the renamed `_run_assignment`, adapted for `TaskResponse` instead of `NextActionResponse`.
- `_post_finished` may retry once or twice on transient HTTP errors (short backoff). If all retries fail, let the exception propagate — the process exits, and the next invocation recovers via the abandoned-task path in `/register`.
- `FatalAssignmentError` is kept: if the task itself has a fatal config error, the worker calls `/finished` with `success=False` then exits with code 2.
- `_render_prompt` loses `assignment_id` from the rendered text. The prompt still includes `session_id`, `iteration`, `workflow_id`, `iteration_dir`, and `goal_check.json output path` for `goal_check` workflows.
- The `poll_interval_seconds`, `finished_retry_attempts`, and `finished_retry_backoff_seconds` parameters are removed from `run_worker_loop`. A small fixed retry (e.g., 2 attempts, 1-second backoff) is baked in for `/finished` only.

### `_render_prompt` change

Remove `Assignment ID: {assignment_id}` line. Keep everything else.

```python
lines = [
    "loopy-loop assignment",
    "",
    f"Goal: {config_snapshot.goal}",
    "Completion criteria:",
    *[f"- {item}" for item in config_snapshot.completion_criteria],
    "Stop criteria:",
    *[f"- {item}" for item in config_snapshot.stop_criteria],
    "",
    f"Session ID: {session_id}",
    f"Iteration: {iteration}",
    f"Workflow ID: {workflow_id}",
    f"Iteration directory: {iteration_dir}",
]
if workflow_id == "goal_check":
    lines.append(f"goal_check.json output path: {iteration_dir / GOAL_CHECK_FILENAME}")
lines.extend(["", "Workflow body:", workflow_prompt])
```

---

## Tests to Remove

- `src/tests/test_lease_reclaim.py` — **delete entirely**. Tests `_reclaim_expired_assignment` which no longer exists. The abandoned-task recovery behavior is covered by new tests in `test_coordinator_app.py`.

---

## Tests to Rewrite

### `src/tests/test_coordinator_app.py`

Replace all existing tests with new ones covering the two-endpoint contract. Key scenarios:

| Test name | What it covers |
|---|---|
| `test_register_returns_run_response` | Fresh state, single `/register` call returns `action=run` with all fields populated |
| `test_register_response_fields` | `workflow_id`, `session_id`, `iteration==1`, `config_snapshot` present and correct |
| `test_register_sets_current_task` | State after `/register` has `current_task` set correctly |
| `test_finished_records_history` | After `/finished`, `history` has one entry, `current_task` is `None` |
| `test_finished_returns_next_run` | With more eligible work, `/finished` returns `action=run` for next iteration |
| `test_finished_stale_mismatch_does_not_mutate` | `/finished` with wrong `session_id`/`workflow_id` returns `action=run` for current task, history unchanged |
| `test_finished_stale_no_current_task_dispatches_fresh` | `/finished` when `current_task` is None acts like `/register`: returns next task |
| `test_register_recovers_abandoned_task` | `/register` with orphaned `current_task` records `error="abandoned"` in history, then dispatches fresh |
| `test_register_stop_when_terminal` | `/register` when `goal_met=True` returns `action=stop` |
| `test_finished_stop_after_max_turns` | After `max_turns` iterations, `/finished` returns `action=stop, stop_reason=max_turns"` |
| `test_stop_precedence_goal_met_over_stop_requested` | Goal met wins over stop requested |
| `test_stop_precedence_matrix` | Parametrize all stop reasons |
| `test_control_signal_sets_unresolvable_error` | `control.json` with `unresolvable_error=true` stops after `/finished` |
| `test_control_json_requires_schema_version` | Missing schema version in `control.json` is ignored |
| `test_invalid_goal_check_output_stops_at_failure_cap` | Invalid `goal_check.json` repeated past cap triggers `goal_check_broken` |
| `test_goal_check_reads_only_current_iteration_artifact` | Wrong path artifact ignored, correct path wins |
| `test_resume_reuses_in_progress_session` | Coordinator restart with `--resume` preserves session_id |
| `test_no_eligible_workflow_stops` | No eligible workflow → `/register` returns `stop, no_eligible_workflow` |

### `src/tests/test_worker.py`

Rewrite to match new loop shape (no `worker_id`, no polling, no assignment_id). Key scenarios:

| Test name | What it covers |
|---|---|
| `test_worker_runs_one_task_and_stops` | Register → run → finished → stop: loop exits cleanly |
| `test_worker_reads_prompt_from_disk` | Prompt text from workflow file appears in rendered prompt |
| `test_worker_uses_config_snapshot_not_disk` | Model from coordinator snapshot, not disk config |
| `test_worker_exits_on_fatal_config_error` | Missing API key: posts finished with `success=False`, exits with code 2 |
| `test_worker_retries_finished_on_transient_error` | Transient HTTP error on `/finished` is retried, then succeeds |
| `test_finished_payload_has_no_assignment_id` | Verify `assignment_id` is absent from the posted JSON |

### `src/tests/test_idempotent_finished.py`

Replace with:

| Test name | What it covers |
|---|---|
| `test_stale_finished_mismatch_does_not_record_history_twice` | Calling `/finished` with stale ids does not double-append history |
| `test_stale_finished_returns_current_task_response` | Stale `/finished` returns current running task's info |
| `test_finished_no_current_task_dispatches_fresh` | `/finished` with no `current_task` dispatches next task (acts as register) |

### `src/tests/conftest.py`

Changes:
- Remove `assignment_factory` fixture.
- Add `current_task_factory` fixture.
- Update `state_factory`: remove `workers` field, rename `active_assignment` → `current_task`.
- Update `history_entry_factory`: remove `assignment_id` and `worker_id` fields.

```python
@pytest.fixture()
def current_task_factory():
    def factory(**overrides: Any) -> CurrentTask:
        data = {
            "workflow_id": "planner",
            "session_id": "goal_20260419_143022_ab12cd34",
            "iteration": 1,
            "started_at": utc_now(),
        }
        data.update(overrides)
        return CurrentTask.model_validate(data)
    return factory
```

---

## Acceptance Criteria

- [ ] `POST /workers/register` no longer exists; `POST /register` exists and returns `TaskResponse`
- [ ] `POST /workers/{worker_id}/next` no longer exists
- [ ] `POST /workers/{worker_id}/finished` no longer exists; `POST /finished` exists and returns `TaskResponse`
- [ ] `TaskResponse` has exactly: `action`, `workflow_id`, `session_id`, `iteration`, `config_snapshot`, `stop_reason`
- [ ] `action` is either `"run"` or `"stop"` (no `"wait"`)
- [ ] `FinishedRequest` has no `assignment_id` field
- [ ] `LoopState` has no `workers` dict and no `active_assignment`; has `current_task: CurrentTask | None`
- [ ] `HistoryEntry` has no `assignment_id` and no `worker_id` fields
- [ ] `WorkerState`, `ActiveAssignment`, `RegisterWorkerResponse`, `NextActionResponse` are gone from `models.py`
- [ ] `DEFAULT_LEASE_SECONDS`, `DEFAULT_POLL_INTERVAL_SECONDS`, `DEFAULT_FINISHED_RETRY_ATTEMPTS`, `DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS`, `WAIT_ACTION` are gone from `models.py`
- [ ] `DEFAULT_LOCK_TIMEOUT_SECONDS` is still in `models.py` (used by `StateStore`)
- [ ] Worker loop has no `while True` polling; it is a simple `while task.action == "run"` loop
- [ ] A `/register` call when `current_task` is set records an `error="abandoned"` history entry before dispatching the next task
- [ ] A `/finished` call with mismatched `session_id` or `workflow_id` does not mutate history and returns the current task's `TaskResponse`
- [ ] All existing tests pass except deleted ones; `test_lease_reclaim.py` is deleted
- [ ] `loopy status` output reflects `current_task` (no `worker_id` in output)
- [ ] `docs/http-contract.md` describes only two endpoints with no `wait` action
- [ ] `README.md` HTTP Contract Summary and CLI Reference match the new design

---

## Docs / README Changes

### `docs/http-contract.md` — full rewrite

New content:

```
# HTTP Contract

loopy-loop exposes exactly two coordinator endpoints.

## POST /register

Request: {} (empty body)

Run response:
{
  "action": "run",
  "workflow_id": "planner",
  "session_id": "ship-landing-page_20260419_143022_ab12cd34",
  "iteration": 3,
  "config_snapshot": { ... },
  "stop_reason": null
}

Stop response:
{
  "action": "stop",
  "stop_reason": "goal_met",
  "workflow_id": null,
  "session_id": null,
  "iteration": null,
  "config_snapshot": null
}

Rules:
- If a current_task is already set (previous worker crashed), /register records it as
  failed (error="abandoned") and then dispatches fresh work.
- If the loop is in a terminal state, /register immediately returns action=stop.

## POST /finished

Request:
{
  "workflow_id": "planner",
  "session_id": "ship-landing-page_20260419_143022_ab12cd34",
  "success": true,
  "text": "done",
  "error": null
}

Response: same shape as /register response.

Rules:
- If session_id+workflow_id does not match current_task, the call is treated as stale:
  state is not mutated, and the current task's run response is returned.
- If current_task is None (no task is active), the coordinator dispatches the next
  available task as if /register had been called.
- The coordinator reads control.json only from the current iteration directory.
- The coordinator reads goal_check.json only from the current goal_check iteration directory.
```

### `README.md` — targeted updates

- **HTTP Contract Summary** section: replace three-endpoint list with two-endpoint list; remove `assignment_id` description; remove mention of `wait` action.
- **CLI Reference** — `loopy coordinator` docstring: "three endpoints" → "two endpoints".
- **CLI Reference** — `loopy worker` docstring: remove mention of polling `/next` and `worker_id`; replace with "calls `/register` once, then loops calling `/finished` until it receives `stop`".
- **`loopy status`** output description: remove "active assignment worker" field mention.
