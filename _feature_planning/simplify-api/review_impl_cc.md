# Implementation Review: Simplify API — Drop Leases, Polling, and Worker Identity

Reviewed files:
- `src/loopy_loop/models.py`
- `src/loopy_loop/coordinator_app.py`
- `src/loopy_loop/worker.py`
- `src/loopy_loop/cli.py`
- `src/tests/test_coordinator_app.py`
- `src/tests/test_worker.py`
- `src/tests/test_idempotent_finished.py`
- `src/tests/conftest.py`
- `docs/http-contract.md`
- `README.md`
- `CHANGELOG.md`

---

## Summary

The implementation is clean, correct, and faithful to the plan. The two-endpoint contract is fully realized, all dead models and constants are gone, the worker loop is the simple `while task.action == "run"` ping-pong described in the plan, and the crash-recovery path works correctly. Test coverage is solid, and the documentation is accurate.

The plan reviewers (review_cc.md, review_codex.md) flagged several "must fix" and "should fix" gaps before implementation. Most of those gaps were addressed in the implementation itself. A small number were not, and a new minor bug was introduced. Details below.

---

## Alignment Check: PASS

Every acceptance criterion from `plan.md` is satisfied:

| AC | Status |
|---|---|
| `POST /workers/register` gone; `POST /register` exists returning `TaskResponse` | PASS — endpoint is `@app.post("/register", response_model=TaskResponse)` |
| `POST /workers/{worker_id}/next` gone | PASS — no such route exists |
| `POST /workers/{worker_id}/finished` gone; `POST /finished` exists returning `TaskResponse` | PASS — endpoint is `@app.post("/finished", response_model=TaskResponse)` |
| `TaskResponse` has exactly `action`, `workflow_id`, `session_id`, `iteration`, `config_snapshot`, `stop_reason` | PASS — six fields, matches plan exactly |
| `action` is `"run"` or `"stop"`, never `"wait"` | PASS — `Literal["run", "stop"]` in models.py |
| `FinishedRequest` has no `assignment_id` | PASS — five fields only |
| `LoopState` has no `workers`, no `active_assignment`; has `current_task: CurrentTask | None` | PASS |
| `HistoryEntry` has no `assignment_id`, no `worker_id` | PASS — seven fields only |
| `WorkerState`, `ActiveAssignment`, `RegisterWorkerResponse`, `NextActionResponse` gone from models.py | PASS |
| Dead constants gone (`DEFAULT_LEASE_SECONDS`, `DEFAULT_POLL_INTERVAL_SECONDS`, `DEFAULT_FINISHED_RETRY_ATTEMPTS`, `DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS`, `WAIT_ACTION`) | PASS |
| `DEFAULT_LOCK_TIMEOUT_SECONDS` retained | PASS — line 11 of models.py |
| Worker loop is `while task.action == "run"`, no polling | PASS |
| `/register` with orphaned `current_task` records `error="abandoned"` before dispatching | PASS — steps 3/4 in `register_worker` mutator |
| `/finished` mismatch does not mutate history or `current_task` | PASS — step 4 returns without any mutation |
| `docs/http-contract.md` has only two endpoints, no `wait` action | PASS |
| `README.md` HTTP Contract Summary and CLI Reference match new design | PASS |
| `loopy status` reflects `current_task`, no `worker_id` | PASS |

---

## CLAUDE.md Compliance

This is a standalone library outside the main Python 3.12 backend. Reviewed against the project-level coding standards that apply here.

- Modern type hints: PASS (`list[str]`, `dict[str, Any]`, `X | None`)
- No `Optional`: PASS
- Named arguments: PASS throughout
- `traceback.print_exc()` in except blocks: PASS in `worker.py`; the `_read_signal` logging-only except blocks in `coordinator_app.py` deliberately use `logger.warning` instead of `traceback.print_exc()` which is correct for signal-file parsing (not a crash path)
- `__init__.py` empty: not reviewed but not changed
- Top-down ordering: PASS — `create_coordinator_app` at top, `CoordinatorService` follows, private helpers at bottom
- No magic numbers: PASS — `_FINISHED_RETRY_ATTEMPTS = 2` and `_FINISHED_RETRY_BACKOFF_SECONDS = 1.0` are module-level named constants in worker.py

---

## Logic Issues

### Bug 1 — MINOR: `_read_signal` applies a schema_version check to `GoalCheckSignal` even though `GoalCheckSignal.schema_version` defaults to `GOAL_CHECK_SCHEMA_VERSION=1`

In `coordinator_app.py` line 398:

```python
if getattr(signal, "schema_version", None) != 1:
    logger.warning("Ignoring unsupported signal schema_version at %s", path)
    return None
```

This check runs for both `ControlSignal` and `GoalCheckSignal`. `GoalCheckSignal.schema_version` defaults to `GOAL_CHECK_SCHEMA_VERSION = 1` in models.py (line 104). If a `goal_check.json` file omits `schema_version`, Pydantic fills in the default value of `1`, so `getattr(signal, "schema_version", None)` returns `1` and the check passes. This is not a bug per se — the behavior is correct — but it is a subtle implicit dependency between the default value and the runtime check. If `GOAL_CHECK_SCHEMA_VERSION` is ever bumped (say, to `2`), the default in the model will be `2` but the hardcoded `!= 1` check in `_read_signal` will reject all valid `GoalCheckSignal` instances that rely on the default. The `ControlSignal` model avoids this by NOT providing a default (the field_validator forces the caller to supply it). The check should use the constant or a class attribute rather than the literal `1`.

This existed before the refactor and was not introduced by this change, but it is worth noting since the review scope includes these files.

### Bug 2 — MINOR: `test_finished_stale_mismatch_does_not_mutate` has a misleading assertion comment

In `test_coordinator_app.py` lines 136–141:

```python
# Returns the current running task's info.
assert stale["action"] == "run"
assert stale["workflow_id"] == reg["workflow_id"]
assert stale["session_id"] == reg["session_id"]
assert stale["iteration"] == reg["iteration"]
```

The comment says "Returns the current running task's info." In this test scenario, the mismatched call arrives while the task from `/register` is still active (`current_task` has not been cleared), so the "current running task" IS the same task from `/register`. The assertion is factually correct. However the comment does not distinguish between two semantically different scenarios:

1. Mismatch while the same task is still active (tested here): returns that same task's info.
2. Mismatch while a DIFFERENT task is active (e.g., coordinator restarted and dispatched a new task): returns the NEW task's info, not the stale caller's task.

The plan reviewer (review_codex.md, Issue 2) called out exactly this ambiguity — that the returned `workflow_id`/`session_id`/`iteration` belong to the CURRENT active task, which in a mismatch scenario may be a different task than the one the stale caller finished. The comment should clarify: "Returns the CURRENT active task's info (may differ from the stale caller's completed task in other scenarios)." This is a documentation issue in the test, not a runtime bug, but it matters for maintainability.

### No-bug observation: abandoned-task iteration_count increment before stop check

The plan reviewer flagged (review_cc.md #1, review_codex.md Issue 1) that abandoning a task may increment `iteration_count` to exactly `max_turns`, which would then correctly trigger a stop. The implementation handles this correctly: in `register_worker`, steps 3 and 4 are ordered so the abandoned cleanup (including `current.iteration_count += 1`) happens before `_stop_response_if_needed`. Test `test_register_terminal_plus_abandoned_task_cleanup_first` validates this. Correctly implemented.

### No-bug observation: `_apply_stop_precedence` called twice per `/finished`

The plan reviewer (review_codex.md Issue 3) noted the double-call pattern. The implementation does call `_apply_stop_precedence` twice: once at step 5e and once inside `_stop_response_if_needed` at step 7. Because `_apply_stop_precedence` is idempotent (it only sets the same values again if conditions haven't changed), this is safe. The code is clear enough that a reader can see both calls and understand their purpose without confusion.

---

## Acceptance Criteria — Reviewer-Flagged Scenarios

The plan reviewers explicitly called out two edge cases that required test coverage. Checking both:

### Terminal state + orphaned current_task (review_cc.md "Must fix #2", review_codex.md Issue 1)

Test: `test_register_terminal_plus_abandoned_task_cleanup_first` in `test_coordinator_app.py` (lines 256–289).

This test sets `state.goal_met = True`, `state.status = "goal_met"`, `state.stop_reason = "goal_met"`, and `state.current_task` to an orphaned task. It then calls `/register` and asserts:
- The abandoned entry is recorded in history (`history[0].error == "abandoned"`)
- The response is `action=stop` with `stop_reason="goal_met"`

PASS — the required scenario is covered.

### Stale `/finished` with no `current_task` + terminal state (review_codex.md Issue 13, review_cc.md missing test gap)

Test: `test_finished_stale_no_current_task_terminal_returns_stop` in `test_coordinator_app.py` (lines 173–198) AND `test_finished_no_current_task_when_terminal_returns_stop` in `test_idempotent_finished.py` (lines 121–148).

Both tests set `state.goal_met = True` with `current_task is None`, call `/finished`, and assert `action=stop, stop_reason="goal_met"`.

PASS — covered in both test files (slight duplication, not harmful).

---

## Test Coverage

### test_coordinator_app.py

Coverage is thorough. Scenarios covered:

- Fresh `/register` returns `run` with correct fields (tested twice from different angles)
- State after `/register` has `current_task` set
- `/finished` records history and dispatches next task
- `/finished` returns next `run` with `iteration==2`
- Stale mismatch `/finished` does not mutate history or `current_task`
- Stale `/finished` with `current_task is None` dispatches fresh
- Stale `/finished` with `current_task is None` in terminal state returns `stop`
- Abandoned task recovery on `/register`
- Terminal + abandoned task: cleanup first, then stop
- Terminal state on `/register` returns `stop`
- `max_turns` stop after `/finished`
- Stop precedence: `goal_met` over `stop_requested`
- Full stop precedence matrix (parametrized)
- Control signal sets `unresolvable_error`
- Invalid `goal_check.json` output stops at failure cap
- Valid `goal_check.json` sets `goal_met`
- No eligible workflow stops
- Resume reuses in-progress session

The plan's recommended test `test_finished_stop_after_max_turns` includes an assertion that `state.current_task is None` after the stop — this is verified at line 330 (`assert updated.current_task is None`). PASS.

One planned test from the plan's table was not implemented:

- `test_control_json_requires_schema_version` — the plan listed this but the implementation omits it. The behavior it would test (a `control.json` with a bad or missing `schema_version` being ignored) is exercised implicitly by `test_control_signal_sets_unresolvable_error` (which uses `schema_version: 1`), but there is no explicit test that a `control.json` with `schema_version: 2` is correctly ignored. This is a minor coverage gap — not a blocker but worth noting.

The plan reviewer (review_cc.md) asked for `test_goal_check_reads_only_current_iteration_artifact` — this was not implemented either. The current test `test_goal_check_sets_goal_met` writes the artifact to the correct path and verifies it is read. A complementary test with an artifact at the wrong iteration path would confirm isolation. This is a low-priority gap.

### test_worker.py

Coverage is thorough:

- One task then stop
- Prompt read from disk, no `Assignment ID` in output
- Config snapshot from coordinator, not disk
- Fatal config error: `/finished` with `success=False`, then `sys.exit(2)`
- Retry on transient `/finished` HTTP error
- All retries exhausted — exception propagates (added, addressed review_cc.md gap #4)
- No `assignment_id` in `/finished` payload

All scenarios from the plan table are present. The "all retries exhausted" test (lines 268–295) was flagged as missing by the plan reviewer and was correctly implemented.

### test_idempotent_finished.py

Coverage:

- Stale `/finished` with same IDs as already-processed task does not double-record history
- Stale `/finished` with mismatched IDs returns current task's `run` response
- `/finished` with `current_task is None` dispatches fresh
- `/finished` with `current_task is None` in terminal state returns `stop`

The fourth test (`test_finished_no_current_task_when_terminal_returns_stop`) was flagged as missing by review_codex.md (Issue 13). It is present. PASS.

One potential clarity issue: `test_stale_finished_mismatch_does_not_record_history_twice` (lines 11–54). The test name says "mismatch" but the second `/finished` call uses the SAME `workflow_id` and `session_id` as the first legitimate call. The scenario is: first `/finished` succeeds and advances the task; the second `/finished` with the same IDs arrives while the NEW task is active, so `session_id + workflow_id` no longer matches `current_task` (which now holds the next iteration). The test correctly validates that history is not double-recorded. However the comment at line 52 says "the stale call dispatches a fresh task but does not double-record the already-processed result" — this is accurate but the word "dispatches" is slightly misleading: the coordinator does NOT dispatch a new task in this branch (the mismatch branch returns the current running task's response without setting a new `current_task`). It is the LEGITIMATE first `/finished` call that already dispatched the next task (by proceeding through steps 5–9). The stale second call simply returns the already-dispatched task's run response. The comment should say "returns the already-dispatched current task's run response" rather than "dispatches a fresh task."

### conftest.py

Factories are correct:

- `current_task_factory` added — matches plan spec exactly
- `history_entry_factory` has no `assignment_id` or `worker_id` fields
- `state_factory` has `current_task: None`, no `workers`, no `active_assignment`
- `snapshot_factory` unchanged (correct)

No `assignment_factory` exists (correctly removed).

---

## Edge Cases

### Covered

- Crash recovery (abandoned task on next `/register`)
- Crash recovery when state is simultaneously terminal
- Stale `/finished` with mismatched IDs (no mutation, returns current task)
- Stale `/finished` with no active task (dispatch fresh or stop if terminal)
- `max_turns` crossing during abandon-task cleanup
- All `/finished` retries exhausted (exception propagates, next `/register` recovers)
- `goal_check_broken` skips normal stop-precedence path

### Not Covered (minor)

1. `control.json` with a bad `schema_version` (e.g., `2`) is silently ignored — no explicit test. The `_read_signal` logic handles it (the `getattr(signal, "schema_version", None) != 1` guard) but there is no test exercising this guard for `ControlSignal`.

2. `goal_check.json` read from the wrong iteration path is ignored — no explicit test. The path construction in `goal_check_path()` should naturally prevent cross-iteration reads, but a test confirming the isolation would be good.

3. The coordinator does not crash or produce duplicate history when `/finished` is called twice in rapid succession with the same valid IDs — the second call goes through the stale-mismatch path (since `current_task` is now the next task after the first call processed the result). The `test_stale_finished_mismatch_does_not_record_history_twice` test covers this indirectly but the connection is not obvious from the test name or docstring.

---

## Documentation

### docs/http-contract.md

Accurate. Two endpoints only. Run and stop response shapes match `TaskResponse` exactly. Rules sections for both endpoints match the implementation logic. No `wait` action. `FinishedRequest` shown without `assignment_id`. PASS.

### README.md

HTTP Contract Summary section: correctly describes two endpoints, no `assignment_id`, no polling, no `wait` action.

CLI Reference — `loopy coordinator` docstring: "exactly two endpoints: `/register` and `/finished`" — PASS.

CLI Reference — `loopy worker` docstring: "Calls `/register` once to receive the first task. Loops calling `/finished` after each completed task until it receives a `stop` response." No mention of `worker_id` or polling. PASS.

`loopy status` description: correctly shows `current_task` with `workflow_id`, `iteration`, `session_id`, `started_at`. No `worker` field in the output. PASS.

### CHANGELOG.md

`CHANGELOG.md` has a `0.2.0 (breaking)` entry documenting all removed models, constants, and fields. The plan reviewer (review_codex.md Issue 9) flagged this as missing from the plan — the implementation correctly added it. The changelog entry is complete and accurate, listing all removed constants/classes and describing the new two-endpoint contract. PASS.

---

## Shortcuts and Notable Omissions

### Not a bug but worth noting: `FatalAssignmentError` path calls `/finished` before `sys.exit(2)`

In `worker.py` lines 44–47, when a `FatalAssignmentError` is raised during task execution, the worker posts `/finished` with the fatal request and then exits. The `/finished` response (the next task or stop) is discarded — the worker exits regardless. This is correct behavior documented in the plan. However the call `_post_finished(...)` in that branch also goes through the retry logic (`_FINISHED_RETRY_ATTEMPTS = 2`). If both retries fail after a fatal error, the exception from `_post_finished` will propagate and the `sys.exit(2)` at line 47 will never execute. The process will exit with an unhandled exception traceback instead of a clean exit code 2. In practice this is an acceptable trade-off (the coordinator will recover via the abandoned-task path on the next `/register`), but it is a subtle behavior difference that is not documented.

### No `test_resume_with_orphaned_current_task_recovers_on_register` test

The plan reviewer (review_codex.md Issue 14) asked for a test that verifies: after resuming a coordinator that has `current_task` set, the next `/register` call cleans up the orphaned task. The existing `test_register_recovers_abandoned_task` covers the logic but does so without going through the `--resume` path. The `test_resume_reuses_in_progress_session` test does use `--resume` but does not set a `current_task` before resuming. The combination is not tested. This is a minor gap — not a blocker.

---

## Verdict: APPROVE

The implementation correctly and completely delivers the planned simplification. All acceptance criteria from the plan pass. The reviewer-flagged "must fix" and "should fix" gaps from both plan reviews were addressed in the implementation. The code is clean, well-commented where comments matter (crash recovery logic in coordinator_app.py, retry rationale in worker.py), and the tests genuinely exercise the described behaviors rather than going through motions.

The issues found are:

1. (MINOR, pre-existing) `_read_signal` hardcodes `!= 1` instead of using `GOAL_CHECK_SCHEMA_VERSION` constant — fragile if version is bumped.
2. (MINOR) The mismatch-branch test comment in `test_finished_stale_mismatch_does_not_mutate` is subtly misleading — "current running task" could mean either the original or a new task depending on context.
3. (MINOR) `test_stale_finished_mismatch_does_not_record_history_twice` comment says "dispatches a fresh task" but the mismatch branch does not dispatch — it returns the already-dispatched current task's run response.
4. (MINOR) `test_control_json_requires_schema_version` is absent (listed in plan's test table).
5. (MINOR) `test_goal_check_reads_only_current_iteration_artifact` is absent (listed in plan's test table).
6. (MINOR) `FatalAssignmentError` path: if both `/finished` retries fail, the process exits via unhandled exception rather than `sys.exit(2)` — undocumented behavior.
7. (MINOR) `test_resume_with_orphaned_current_task_recovers_on_register` is absent — the combination of `--resume` and an orphaned `current_task` is not exercised end-to-end.

None of these require a fix before shipping. Items 2 and 3 are comment-only fixes. Items 4, 5, and 7 are missing tests for behavior that is already covered by other tests. Items 1 and 6 are pre-existing or edge-case behavioral gaps that can be addressed in follow-up.
