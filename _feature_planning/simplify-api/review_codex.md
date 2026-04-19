# CODEX Review: Simplify API — Drop Leases, Polling, and Worker Identity

Reviewed against:
- `plan.md` and `questions.md`
- `coordinator_app.py`, `models.py`, `worker.py`, `cli.py`, `state_store.py`
- All test files in `src/tests/`

---

## Plan Review Summary

### Requirements Coverage

Addressed:
- Drop `POST /workers/{worker_id}/next` entirely
- Drop leases and polling
- Both `/register` and `/finished` return `TaskResponse` directly
- Remove `worker_id`, `WorkerState`, `ActiveAssignment.lease_seconds`, `assignment_id` from models
- Crash recovery via `/register` detecting orphaned `current_task`
- Worker loop simplification to ping-pong pattern
- `cli.py` `status` command update
- `docs/http-contract.md` and `README.md` updates planned
- All deletions and modifications listed

Partial:
- `test_cli.py`: plan says "minor: if it references `workers` in status output, update that assertion" — the existing test does NOT assert on `workers` in the status output (only on `iteration_count`), but it does call `state_factory()` which still has `active_assignment` and `workers` keys. After the refactor `state_factory` will change; the existing test should still pass but this needs verification. The plan is too casual here.
- `FatalAssignmentError` handling in the new worker: the plan says "the worker calls `/finished` with `success=False` then exits with code 2" but does not explicitly spell out that `FatalAssignmentError.request` must be updated to use the new `FinishedRequest` shape (no `assignment_id`). This is implicit and easy to miss.

Missing:
- `CHANGELOG.md` — not mentioned at all (see issue #9 below).
- `test_cli.py` `test_status_and_stop_commands` currently asserts `"iteration_count: 0" in status_result.output` — this will still pass. However the `status` command currently prints `active_assignment: none` or `active_assignment: {workflow_id} (iteration X, worker Y)`. After refactor it should print `current_task`. The plan says to update `cli.py` but does NOT list `test_cli.py` in "Modify" — it only lists it in "No change needed (minor: if it references `workers` in status output, update that assertion)". The test does not directly assert on `active_assignment` text so it will not break, but the plan's treatment of `test_cli.py` is imprecise.

---

### Issue-by-Issue Analysis

---

**Issue 1 — BLOCKER: `/register` crash recovery + terminal state — wrong order when `current_task` set AND terminal**

Plan step 4 says:
> Call `_stop_response_if_needed(state)`. If a stop condition is active, return `TaskResponse(action="stop", ...)`.

But plan step 3 says to do abandoned-task cleanup first, increment `iteration_count`, then step 4 checks stop. This matches Q3's recommendation ("always do abandoned-task cleanup first, then check stop conditions"). So the stated plan order IS correct.

However there is a subtle bug in the interaction: `_apply_stop_precedence` (used in `_stop_response_if_needed`) already checks `state.iteration_count >= state.max_turns`. If the abandoned-task recovery increments `iteration_count` to exactly `max_turns`, step 4 will return `stop, max_turns` immediately — without ever dispatching the next task. This is the CORRECT behaviour, but the plan never explicitly acknowledges this interaction. More critically: the plan does NOT mention that the incremented `iteration_count` should be the value used for the stop-condition check. This is correct incidentally (because step 3 increments before step 4), but without an explicit note implementors may accidentally check stop conditions using the pre-increment count.

Suggested fix: Add a note in the plan that the abandon cleanup (step 3) must complete before stop condition evaluation (step 4) specifically because `iteration_count` may cross `max_turns` during cleanup.

---

**Issue 2 — BLOCKER: Stale `/finished` with `current_task = None` dispatches a new task — but `iteration_count` is NOT incremented**

The plan says (final decision): "If `current_task` is `None`, dispatch as if it were a fresh `/register`."

In the `/register` path, dispatch means: check stop, call `choose_next_workflow`, set `current_task`, return run response. `iteration_count` is NOT incremented at dispatch time — it is incremented when `/finished` is processed.

This is fine. But consider the scenario:
1. Worker calls `/finished` legitimately. Coordinator records history, increments `iteration_count`, clears `current_task`, dispatches next task, sets new `current_task`, returns run response.
2. Worker gets a transient network error and never sees the response.
3. Worker retries `/finished` with the old (now stale) IDs.
4. Coordinator sees `current_task` IS set (to the NEW task dispatched in step 1). The `session_id` + `workflow_id` mismatch triggers "stale mismatch" branch (step 4 in `/finished` logic), NOT the `current_task = None` branch.
5. Plan says: return `_run_response(current_task=current_task, ...)` — i.e., tell the worker to run the NEW task.

This is actually a significant correctness problem. The worker sent `/finished` for task A, gets told to run task B. The worker loop calls `_post_finished` expecting a `TaskResponse`, which it will receive (it says run task B). The worker then tries to execute task B while task B's `current_task` was set by what the coordinator thinks was a legitimate dispatch. This means the new task gets run WITHOUT the worker ever having called `/register` for it, and a subsequent legitimate `/finished` for task B will match correctly. In the single-worker model this is safe — the worker will run B and call `/finished(B)` which will match. However it is a behaviour the plan does not document clearly, making it a maintenance hazard.

More importantly: the plan's test `test_finished_stale_mismatch_does_not_mutate` says "returns `action=run` for current task, history unchanged" — which is correct per the spec, but the test description is misleading because "current task" here refers to the NEXT task (B), not the task the worker originally ran (A). The test should be explicit about this.

Suggested fix: Add an explanatory note to the plan and to the test docstring that in the mismatch case the coordinator returns the NEWLY assigned task, not a repeat of the completed task.

---

**Issue 3 — BLOCKER: `_apply_stop_precedence` is called inside `/finished` BOTH explicitly AND again inside the subsequent dispatch**

In the current `coordinator_app.py`, `_dispatch_next_action` calls `_reclaim_expired_assignment` then `_stop_response_if_needed` (which calls `_apply_stop_precedence`). In the plan's new `/finished` logic, step 5e says "call `_apply_stop_precedence(state)`" and then step 7 says "check stop conditions". If the new dispatch helper (`_stop_response_if_needed`) also calls `_apply_stop_precedence`, then `_apply_stop_precedence` is called twice per `/finished` invocation. Calling it twice is idempotent for the status/stop_reason mutation (it will set the same values again), but it means the plan introduces a hidden redundancy that can confuse future readers.

The existing code already has this pattern (look at `finish_assignment`: it applies precedence in the unresolvable_error/goal_met block, then calls `_dispatch_next_action` which calls `_stop_response_if_needed` which calls `_apply_stop_precedence` again). The plan preserves this double-call pattern without comment.

Suggested fix: Either (a) document that `_apply_stop_precedence` is intentionally idempotent and double-calls are safe, or (b) restructure so stop-condition evaluation happens exactly once per request. Option (a) is simpler.

---

**Issue 4 — BLOCKER: `goal_met` status/stop_reason set inside goal_check handling — plan omits this**

In the CURRENT `finish_assignment` code (lines 150-153 of `coordinator_app.py`):
```python
current.goal_check_consecutive_failures = 0
if goal_signal.goal_met:
    current.goal_met = True
    current.stop_reason = "goal_met"
    current.status = "goal_met"
```

The current code sets `current.stop_reason` and `current.status` directly here, before calling `_apply_stop_precedence`. The plan (step 5c) says: "if `goal_signal.goal_met`: set `state.goal_met = True`." It does NOT say to also set `stop_reason` and `status` here. It relies on step 5e's `_apply_stop_precedence` to set those. 

This is actually a cleaner design (let `_apply_stop_precedence` be the single place that sets `status` and `stop_reason`), but the plan needs to be explicit that the new code intentionally drops the direct `stop_reason`/`status` assignment in the goal_check branch. Otherwise an implementor reading the existing code will copy those assignments over and create an inconsistency.

Suggested fix: Add a note: "Unlike the current code, do NOT set `state.stop_reason` or `state.status` directly inside the goal_check branch. `_apply_stop_precedence` (step 5e) handles that."

---

**Issue 5 — MINOR: `CurrentTask.iteration` semantics inconsistency**

The plan says in step 6 of `/register`:
> Set `current_task = CurrentTask(..., iteration=state.iteration_count + 1, ...)`

And `HistoryEntry.iteration` will be set from `current_task.iteration` when recording history. But `iteration_count` is incremented AFTER the task finishes. So if `iteration_count = 0`, `current_task.iteration = 1`, and after finishing, `iteration_count` becomes 1. This matches the existing code (`assignment.iteration = state.iteration_count + 1`). Good.

However the plan is self-inconsistent in the `TaskResponse` return: it says `iteration: int | None` is "set when action == run". The returned `TaskResponse` should carry `iteration = current_task.iteration` (i.e., the 1-based slot the worker is executing). This is obvious from context but the plan never explicitly says "return `current_task.iteration` in the run response", it just shows the field in the model. Minor but worth calling out.

---

**Issue 6 — MINOR: `test_fresh_run_archive.py` — plan says "minor update (no `workers` in state)" but the test calls `state_factory(status="failed", ...)`**

The existing `state_factory` fixture creates a `LoopState` with `workers: {}` and `active_assignment: None`. After the refactor, `state_factory` will create a `LoopState` with `current_task: None` (no `workers`, no `active_assignment`). The test itself does not reference `workers` directly, so it will pass after `state_factory` is updated. The plan correctly identifies this as a minor update. No bug, but the plan's explanation is thin.

---

**Issue 7 — MINOR: `test_goal_check_gate.py` and `test_must_follow_success.py` use `history_entry_factory`**

Both tests use `history_entry_factory` which currently produces `HistoryEntry` objects with `assignment_id` and `worker_id` fields. After the refactor, `HistoryEntry` drops those fields and `history_entry_factory` must be updated accordingly. The plan lists these files as "No change needed" but does NOT list `history_entry_factory` update as affecting them.

In practice, since both tests pass `HistoryEntry` objects into `choose_next_workflow` (the scheduler), and the scheduler only reads `workflow_id`, `success`, and `iteration` from history entries, the tests will pass after the fixture is updated. But the plan is misleading: both files DO need their fixtures to be regenerated (via the updated `history_entry_factory` in `conftest.py`), even if the test files themselves do not change. The plan does not make this clear.

More specifically: if `history_entry_factory` is updated in `conftest.py` to remove `assignment_id` and `worker_id`, the existing test files will work without modification because they never access those fields from the returned objects. So the plan's "No change needed" verdict is correct at the file level, but the reasoning is incomplete.

---

**Issue 8 — MINOR: `test_harness_runner.py` — "No change needed" is correct but untested in the plan**

`test_harness_runner.py` imports nothing from the removed models (`WorkerState`, `ActiveAssignment`, `NextActionResponse`, etc.) and does not reference `assignment_id` or `worker_id`. The plan's "No change needed" verdict is correct. However the plan does not verify this claim — it simply lists the file without analysis.

Specifically: `test_harness_runner.py` uses `snapshot_factory` and `repo_root` fixtures from `conftest.py`. Neither changes incompatibly. No action needed, but the plan should acknowledge WHY these are safe rather than just listing them.

---

**Issue 9 — MINOR: `CHANGELOG.md` not mentioned**

The repo has a `CHANGELOG.md` (standard for libraries). A breaking API change of this magnitude — dropping three endpoints, all worker identity, all lease parameters — is a semver major or at minimum a prominent changelog entry. The plan does not mention updating `CHANGELOG.md` at all.

Suggested fix: Add `CHANGELOG.md` to the "Modify" list with a note to add a breaking-change entry.

---

**Issue 10 — MINOR: Backwards compatibility — zero mention**

The plan states this is a simplification for a system that "runs one worker at a time in practice." However it is completely silent on:
- Whether any external callers (scripts, CI pipelines, other repos) call the old endpoints
- Whether there is a migration path for existing `state.json` files that contain `workers`, `active_assignment`, `assignment_id` in history entries

The `state.json` migration deserves a sentence. If an existing `state.json` has `active_assignment` and `workers`, Pydantic will fail to parse it with the new `LoopState` model (because `active_assignment` and `workers` are removed fields, and Pydantic by default does NOT allow extra fields).

Wait — Pydantic v2 by default IGNORES unknown fields on model_validate. So if `state.json` has `workers: {...}` and `active_assignment: {...}`, but the new `LoopState` model has neither field, Pydantic will silently drop them and `current_task` will default to `None`. This means existing `state.json` files will be silently "upgraded" on first read — the old assignment is lost with no history entry. For a running (non-terminal) state this could cause silent data loss: a running task (captured in `active_assignment`) becomes invisible, and no abandoned-task recovery fires because `/register` was not called.

This is only a concern if the coordinator is upgraded while a task is mid-flight (i.e., worker is running but the coordinator process is restarted with new code). In that case the old `active_assignment` data is lost without a history record. The plan should acknowledge this and either (a) write a one-time migration that converts `active_assignment` to `current_task` on startup, or (b) document it as an acceptable limitation.

Suggested fix: Add a migration note: "If upgrading from the old API, any in-flight `active_assignment` will be silently dropped on first read. Either drain all workers before deploying, or add a startup migration step."

---

**Issue 11 — MINOR: `FatalAssignmentError` and the new `FinishedRequest` shape**

The existing worker code constructs `FatalAssignmentError` with a `FinishedRequest` that includes `assignment_id`. The new `FinishedRequest` drops `assignment_id`. The plan says "`FatalAssignmentError` is kept" and implies the request construction changes, but does NOT explicitly list the change to `FinishedRequest` construction inside `_run_task` (the renamed `_run_assignment`). An implementor could forget to remove `assignment_id` from the `FinishedRequest` construction in the fatal error path.

Suggested fix: In the worker section, explicitly state: "The `FinishedRequest` constructed for fatal errors no longer includes `assignment_id`."

---

**Issue 12 — MINOR: Worker retry constants — naming convention**

The plan (Q2 answer) recommends naming the constants `_FINISHED_RETRY_ATTEMPTS = 2` and `_FINISHED_RETRY_BACKOFF_SECONDS = 1.0` in `worker.py` with a leading underscore. Leading underscore for module-level constants is unusual in Python — the convention is either ALL_CAPS without underscore (public) or `_ALL_CAPS` for "private" but this is rarely done in practice. The CLAUDE.md code standards do not address this specific case.

The bigger point: the plan says "not in `models.py`, since they are internal to the worker, not part of the shared model layer." This reasoning is sound and correct. The naming quirk is trivial.

No action needed; the plan is defensible here.

---

**Issue 13 — MINOR: Test `test_finished_stale_no_current_task_dispatches_fresh` — edge case gap**

The plan includes a test that calls `/finished` when `current_task` is `None` and expects a run response (fresh dispatch). But there is a missing companion scenario: what if `/finished` is called when `current_task` is `None` AND the state is terminal (e.g., `goal_met = True`)? The plan's dispatch-as-register logic should return `stop` in that case, but there is no test for it.

This is a gap in test coverage for the "stale `/finished` + terminal state" path.

Suggested fix: Add `test_finished_no_current_task_terminal_returns_stop` to `test_idempotent_finished.py`.

---

**Issue 14 — MINOR: Test `test_resume_reuses_in_progress_session` — needs updating**

The current test (in `test_coordinator_app.py`) calls `client.post("/workers/register")` and then `client.post(f"/workers/{worker_id}/next")` and checks `next_response["session_id"]`. After the refactor, the `/workers/register` and `/workers/{worker_id}/next` endpoints are gone. The plan says this test should be updated as part of the "full rewrite" of `test_coordinator_app.py`. But the new test table in the plan lists `test_resume_reuses_in_progress_session` — which correctly validates that after resuming, `/register` returns the same `session_id`. This is covered.

However, one thing the new plan test does not explicitly test: that `--resume` with an in-progress state that has `current_task` set still resumes (i.e., the resume path does not crash when a `current_task` exists). The abandoned-task cleanup only fires at the next `/register` call, not at coordinator startup. So after resume, if a worker calls `/register`, it will clean up the old task. The plan should add a test: `test_resume_with_orphaned_current_task_recovers_on_register`.

---

**Issue 15 — MINOR: `cli.py` `status` command — plan says "update" but does not specify the exact new output format**

The plan says "update `status` command to use `current_task`" and "update CLI docstring". The current `status` command prints:
```
active_assignment: planner (iteration 1, worker worker_1)
```

The plan does not specify what the new format should be, e.g.:
```
current_task: planner (iteration 1)
```

This is a documentation gap in the plan. An implementor will make a reasonable choice but the plan should be explicit.

Suggested fix: Add a sample output snippet for the new `loopy status` format.

---

**Issue 16 — MINOR: `_run_response` helper signature change not addressed**

The current `_run_response` takes `assignment: ActiveAssignment`. After the refactor it should take `current_task: CurrentTask`. The plan alludes to replacing `_run_response` but does not explicitly state this signature change. The plan describes removing `_dispatch_next_action`, `_require_worker`, `_reclaim_expired_assignment` and replacing with "simpler helpers", but it does not list what those simpler helpers are or their signatures.

This leaves the implementation underspecified. An implementor has to infer what `_run_response` looks like in the new code from context.

Suggested fix: Add a brief description of the new helper functions to the plan, similar to how the old ones are described.

---

**Issue 17 — MINOR: `_post_next` removal from worker — test coverage**

The existing `test_worker.py` test `test_worker_reads_prompt_from_disk_and_retries_finished` mocks HTTP calls via a `responses` list. The list currently includes: `{worker_id: worker_1}` (register), `run_payload` (next), `httpx.ConnectError` (transient on finished), `finished_payload` (stop). After the refactor, the register call directly returns a run response, so the mock list changes to: `run_payload` (register), `httpx.ConnectError` (transient on finished), `finished_payload` (stop). The plan says "full rewrite" of `test_worker.py` — which covers this — but an explicit note that the mock sequence changes would help.

The new test `test_worker_retries_finished_on_transient_error` in the plan covers this correctly.

---

**Issue 18 — MINOR: `test_finished_payload_has_no_assignment_id` — test validity**

The plan includes `test_finished_payload_has_no_assignment_id` as a test. This is a good regression guard. However it tests that the JSON posted to `/finished` does not contain `assignment_id`. Since `FinishedRequest` will not have an `assignment_id` field, Pydantic's `model_dump()` will naturally exclude it. The test is essentially testing that Pydantic serialization works correctly, which is low-value. It would be more valuable to test that the coordinator does NOT 422-reject a request without `assignment_id` (i.e., the field is truly gone from the request schema). Consider replacing with an integration test that sends a valid `FinishedRequest` JSON without `assignment_id` and verifies the coordinator accepts it.

---

**Issue 19 — MINOR: `questions.md` Q1 decision vs. plan body inconsistency**

`questions.md` Q1 recommends Option B ("dispatch next task immediately when `current_task` is None"). The plan body initially vacillates between options before landing on Option B. The plan body's wandering through Options A, B, and C (the "Wait — there is a subtlety" paragraph) is confusing and looks like unfinished editing. It should be cleaned up to state the decision directly without showing the reasoning journey.

No functional impact, but it creates confusion for an implementor reading the plan.

---

**Issue 20 — MINOR: `iteration` field on `CurrentTask` is 1-based but plan never states this explicitly**

The plan says `iteration=state.iteration_count + 1` in the dispatch step. This means iteration is 1-based. But `CurrentTask.iteration` (and `HistoryEntry.iteration`) are used for things like directory names (`0001_planner`, `0002_goal_check`). The zero-padding format is handled in `sessions.py` (`ensure_iteration_dir`). The plan does not check whether `sessions.py` needs any updates.

Looking at `sessions.py` (not read in full but referenced): it uses `iteration` to create directory names. If the semantics of `iteration` change (e.g., from "slot number" to something else), `sessions.py` would need updating. But since the plan keeps `iteration = iteration_count + 1` semantics identical to the existing `assignment.iteration = iteration_count + 1`, no change is needed. The plan's "No change needed" for `sessions.py` is correct.

---

### Complexity Assessment

Overall Complexity: Appropriate. The plan correctly identifies that the existing lease/poll/worker-identity machinery is pure overhead for a single-worker use case and proposes a minimal replacement. The new design is genuinely simpler.

Areas of Concern:
- The stale `/finished` with `current_task = None` dispatching as a fresh `/register` adds a non-obvious code path. It is the right call (Option B), but the plan's self-editing in the body makes it look uncertain.
- Double-call to `_apply_stop_precedence` (Issue 3) is inherited from the existing code and preserved without comment.

Simplification Opportunities:
- The plan could make the stale-finished dispatch path even simpler by extracting a `_dispatch_next_or_stop(state)` helper used by both `/register` (after cleanup) and `/finished` (both the normal and stale paths). This would also eliminate the double `_apply_stop_precedence` problem.

---

### Testing Strategy

Coverage: Needs Improvement

Missing test scenarios:
1. (Issue 13) `test_finished_no_current_task_terminal_returns_stop` — stale `/finished` when state is terminal
2. (Issue 14) `test_resume_with_orphaned_current_task_recovers_on_register` — resume with existing `current_task`
3. `test_register_when_terminal_with_current_task_cleans_up_then_stops` — Issue 1 scenario (cleanup first, then stop if iteration_count crosses max_turns during cleanup)

The plan's listed test table for `test_coordinator_app.py` covers the happy path and most edge cases well. The gaps are specifically around the interaction between crash recovery and terminal state transitions.

---

### Documentation Plan

Completeness: Partial

Gaps:
- `CHANGELOG.md` not mentioned
- New `loopy status` output format not specified
- New helper function signatures in `coordinator_app.py` not documented
- Migration behaviour for existing `state.json` with old schema fields not documented

Suggestions:
- Add a migration note for deployments upgrading from the old API
- Add explicit output format for `loopy status` in the plan

---

### Overall Recommendation

APPROVE_WITH_FIXES — the plan is well-structured and the design decisions are sound. The core simplification is correct. However, the following must be addressed before implementation begins:

1. (Blocker Issue 1) Add explicit note that abandoned-task `iteration_count` increment must precede stop-condition evaluation, and that crossing `max_turns` during cleanup is the correct terminal outcome.

2. (Blocker Issue 2) Add a note and test clarifying that a "stale mismatch" `/finished` returns the CURRENT (newly assigned) task info, not a repeat of the completed task — and that this is safe in the single-worker model.

3. (Blocker Issue 3) Document that `_apply_stop_precedence` is intentionally idempotent and will be called twice per `/finished` request; or refactor to call it once via a shared helper.

4. (Blocker Issue 4) Explicitly state that the new code does NOT set `stop_reason`/`status` directly inside the goal_check branch — `_apply_stop_precedence` handles it.

5. (Issue 10) Add a migration note for in-flight `active_assignment` data that will be silently dropped when upgrading with a running state.

6. (Issue 9) Add `CHANGELOG.md` to the "Modify" list.

7. (Issue 13) Add `test_finished_no_current_task_terminal_returns_stop` to the planned test table.

8. (Issue 15) Specify the exact new output format for `loopy status`.

9. (Issue 16) Describe the new helper function signatures in `coordinator_app.py`.

None of the blockers represent a fundamental flaw in the design — they are gaps in the plan document that could cause implementation mistakes. The design itself is correct.
