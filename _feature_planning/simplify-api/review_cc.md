# Plan Review: Simplify API — Drop Leases, Polling, and Worker Identity

## Plan Review Summary

### Requirements Coverage

Addressed:
- Drop `POST /workers/{worker_id}/next` entirely
- Drop `POST /workers/register` returning `worker_id`; replace with `POST /register` returning `TaskResponse`
- Drop `POST /workers/{worker_id}/finished`; replace with `POST /finished` returning `TaskResponse`
- Remove `WorkerState`, `ActiveAssignment`, `RegisterWorkerResponse`, `NextActionResponse` from models
- Remove leases, polling, `worker_id` concept
- Crash recovery via abandoned-task detection on `/register`
- Stale `/finished` handling (Option B: dispatch fresh)
- Worker loop simplified to ping-pong pattern
- All constants to remove and keep are identified
- `HistoryEntry` updated (drops `assignment_id`, `worker_id`, keeps `session_id`)
- `LoopState` updated (`workers` dict removed, `active_assignment` -> `current_task`)
- CLI `status` command updated
- Documentation updated (http-contract.md, README.md)
- Test files to delete, rewrite, and keep are all enumerated

Partial:
- `test_fresh_run_archive.py`: The plan says "minor update (no `workers` in state)" but does not enumerate what the actual change is. The existing test uses `state_factory()` which includes `workers: {}`. After the refactor, `state_factory` will not have `workers` in the default data, so the test may pass unchanged if the factory is updated correctly. However the plan should explicitly state this is a no-op if the conftest update handles it. Currently listed as needing change but no detail given.
- `test_cli.py`: The plan lists this as "no change needed (minor: if it references `workers` in status output, update that assertion)". The current `test_status_and_stop_commands` test does NOT assert on the `active_assignment` line in status output — it only checks `iteration_count: 0`. However `cli.py`'s `status` command currently reads `state.active_assignment`, which must be changed to `state.current_task`. This is a required source change (not just test change) that must be made to `cli.py` and is covered in the plan, but the note in the "no change needed" section for `test_cli.py` is misleading.

Missing:
- `test_goal_slug_validation.py` is not mentioned anywhere in the plan (appears to be independent; likely not affected, but the plan should confirm it).
- `test_harness_runner.py` is not mentioned. It is likely unaffected but should be listed under "No change needed" for completeness.
- The plan does not mention whether `pyproject.toml` or any entry points need updating. The three old URL paths are hardcoded only in `coordinator_app.py` and `worker.py`, so this is probably fine, but worth a sentence.

### Complexity Assessment

Overall Complexity: Appropriate

The refactor is a genuine simplification. The removed machinery (leases, worker registry, `wait` action, polling loop, assignment ID idempotency) is replaced by a much leaner model. The resulting surface area is demonstrably smaller.

Areas of Concern:

1. The stale `/finished` with `current_task is None` dispatching a fresh task (Option B) has a subtle correctness issue. The plan describes this path: "if `current_task` is `None`, dispatch as if it were a fresh `/register`". This means the coordinator will set a new `current_task` and increment `iteration_count` by 1 when the task finishes. But the stale caller (e.g., a worker that already ran and then called `/finished` twice due to a retry) will now receive a `run` action and execute a task that was not intended. In the new single-worker model this is unlikely in practice, but it is a semantic difference from Option A that the plan glosses over. The plan should state clearly: "stale `/finished` with no `current_task` is expected only if the coordinator restarted and recovered; in that case the worker calling it is the correct active worker, and dispatching fresh work is correct." If there can be two concurrent workers (even accidentally), Option B can cause duplicate work. The plan confirms there is only one worker at a time, so Option B is fine — but this reasoning should be explicit in the plan text.

2. The `/finished` mismatch branch (step 4: `session_id` or `workflow_id` mismatch with non-None `current_task`) returns the current task's run response to the mismatched caller. The plan says "Return `_run_response(current_task=current_task, snapshot=state.config_snapshot)`". The mismatched caller is a stale worker that already finished its task; telling it to re-run `current_task` (which belongs to a different worker's context in a hypothetical multi-worker scenario, or to the current task in a hypothetical concurrent call scenario) is technically a no-op since the single worker will have already moved on. This is fine in practice, but the plan should note that the returned `workflow_id`/`session_id`/`iteration` in the response will be for the CURRENT task, not the stale caller's task. A well-written worker would ignore this response after exiting anyway (since it called `/finished` already and its loop is done), so this is not a bug, just worth documenting.

3. The `_has_invalid_control_output` helper currently takes `active_assignment: ActiveAssignment`. After the refactor it should take `current_task: CurrentTask`. The plan states this implicitly (it says to replace helpers) but never explicitly maps the old parameter name to the new one. The implementer should not miss this.

Simplification Opportunities:
- The plan correctly removes configurable retry parameters from `run_worker_loop` and hardens them as module-level constants in `worker.py`. This is the right call.
- No over-engineering detected elsewhere.

### Testing Strategy

Coverage: Needs improvement in two areas.

Missing test scenarios:

1. **`/register` when state is terminal AND `current_task` is set simultaneously.** The plan says in Q3 to always do abandoned-task cleanup first, then check stop. The test `test_register_stop_when_terminal` covers the simple "terminal but no current_task" case. A test covering "terminal state + orphaned current_task" is absent from the test table. This edge is explicitly resolved in Q3 but has no corresponding test.

2. **`/finished` when stop condition fires mid-processing (goal_check_broken path).** The plan has step 6 as a special case: if `stop_reason == "goal_check_broken"`, return immediately without calling `_dispatch_next_action`. The existing `test_invalid_goal_check_output_stops_at_failure_cap` covers this, but the new test table should confirm it is included. The plan lists it as a test to rewrite — good. But `test_finished_stop_after_max_turns` should also verify that `current_task` is cleared (set to `None`) before returning the stop response, which is not explicitly stated in the test description.

3. **Worker retry actually exhausts and raises.** The plan describes: if all retries fail, the exception propagates and the process exits. `test_worker_retries_finished_on_transient_error` only tests the success case (retry, then success). A test for "all retries exhausted" is not in the plan's test table. This is a minor gap but a real behavior path.

4. **`test_finished_stale_no_current_task_dispatches_fresh`** (listed in `test_idempotent_finished.py`) correctly covers Option B, but the description "acts as register" should verify that the response contains a valid `workflow_id` and `iteration`, not just `action=="run"`. This is a test quality note, not a missing test.

5. The `test_worker_reads_prompt_from_disk_and_retries_finished` test in the current `test_worker.py` injects a `ConnectError` on the first `/finished` call and expects success on retry. The new plan's test table mentions `test_worker_retries_finished_on_transient_error` as a new test. The implementer should not forget to remove the old combined test and replace it with the two focused ones listed in the plan.

6. **`test_finished_payload_has_no_assignment_id`** is listed in the worker test table. Good catch — this is exactly the type of regression check that prevents silent reintroduction of removed fields. Keep it.

Recommendations:
- Add `test_register_recovers_abandoned_task_when_terminal` to `test_coordinator_app.py` covering the Q3 scenario.
- In `test_finished_stop_after_max_turns`, assert `state.current_task is None` after the call.
- Add `test_worker_finished_all_retries_exhausted` to `test_worker.py`.

### Documentation Plan

Completeness: Complete

The plan rewrites `docs/http-contract.md` and updates README.md. Both changes are specific and described with enough detail to execute.

Gaps:
- None significant. The doc rewrite accurately reflects the new two-endpoint contract.

Minor suggestion: the `docs/http-contract.md` rewrite example shows `"stop_reason": null` in the run response. This should be explicitly confirmed in the `TaskResponse` model — `stop_reason` defaults to `None`, which is correct, but the doc example is the external contract and should be accurate. It is accurate as written.

### Correctness Issues (Specific Bugs)

**Bug 1: Off-by-one in iteration numbering.**
In step 6 of `/register`, the plan sets:
```python
current_task = CurrentTask(..., iteration=state.iteration_count + 1, ...)
```
Then in step 5g of `/finished`:
```python
state.iteration_count += 1
```
And for the abandoned-task path in `/register` step 3:
```python
# record history entry for abandoned task
state.iteration_count += 1
# clear current_task
```
Then step 6 sets the next task's `iteration = state.iteration_count + 1`.

This means: after abandonment, `iteration_count` is incremented, then the new task gets `iteration = iteration_count + 1`. So if the abandoned task was iteration 1 and `iteration_count` was 1 before abandonment (because it was set during assignment dispatch), the abandoned task gets recorded correctly, `iteration_count` becomes 1 after increment, and the new task gets `iteration = 2`. Wait — let's trace more carefully.

The original `/register` sets `current_task.iteration = state.iteration_count + 1` BEFORE incrementing `iteration_count`. `iteration_count` is only incremented in `/finished`. So at dispatch time: `current_task.iteration = 0 + 1 = 1`, `iteration_count` stays 0. When finished is called: history entry with `iteration=1`, then `iteration_count` becomes 1. Next dispatch: `current_task.iteration = 1 + 1 = 2`.

Abandoned path in next `/register`: the orphaned `current_task.iteration` is whatever was set (e.g., 1). Record it to history. Then `iteration_count += 1` (0 → 1). Then new task: `iteration = 1 + 1 = 2`.

This is consistent. No off-by-one bug exists here. The plan is correct.

**Bug 2: The stale `/finished` mismatch path (step 4) may expose internal state.**
When `current_task` is set but the `session_id`/`workflow_id` don't match, the plan returns the CURRENT task's run response. This leaks `workflow_id`, `session_id`, `iteration`, and `config_snapshot` of the current (different) task to the stale caller. In a single-worker model, the stale caller is the same worker process (a retry), so this is harmless. But if interpreted strictly as an HTTP API, this is an unintended information disclosure. The plan should note this is acceptable because loopy-loop is repo-local with no auth, so there are no confidentiality concerns.

**Bug 3: Missing check — `_has_invalid_control_output` and `_has_unresolvable_error_signal` in `/finished` use `current_task` fields.**
The current code uses `active_assignment.session_id`, `active_assignment.iteration`, `active_assignment.workflow_id` to compute paths. The plan says to adapt these helpers for `CurrentTask`. The `CurrentTask` model has `session_id`, `iteration`, `workflow_id` — same fields — so the path computation is straightforward. The plan does not explicitly state this, but it is implied. The implementer should verify the field names match before blindly refactoring.

**Bug 4: `_apply_stop_precedence` is called in `_stop_response_if_needed` which mutates state.**
The current `_stop_response_if_needed` calls `_apply_stop_precedence` which can set `state.status` and `state.stop_reason`. In `/register`, step 4 calls `_stop_response_if_needed`. If it returns not-None, the function returns immediately without writing a new `current_task`. The state mutation (setting `status` and `stop_reason`) has already happened inside the mutator, so it will be persisted by `StateStore.mutate`. This is correct behavior and mirrors the existing code. The plan is correct here.

**Non-bug observation: `test_stop_precedence_matrix` needs updating.**
The existing test uses `client.post(f"/workers/{worker_id}/next")` to trigger stop. After the refactor, the equivalent is `client.post("/register")`. The plan lists this test for rewrite — good. Just making sure the implementer knows the `no_eligible_workflow` case requires a `repo_builder(workflows=...)` variant where only `goal_check` is defined (which has `not_before_iteration: 1`), so at iteration 0 no workflow is eligible. The existing test already has this pattern; it must be preserved in the rewrite.

### Acceptance Criteria Review

The ACs are verifiable and complete with one gap:

- The AC "A `/finished` call with mismatched `session_id` or `workflow_id` does not mutate history and returns the current task's `TaskResponse`" is correct but should also include "and does not change `current_task`". Without that, a misbehaving implementation could clear `current_task` and still satisfy the stated AC.

- The AC "All existing tests pass except deleted ones; `test_lease_reclaim.py` is deleted" is slightly imprecise. `test_idempotent_finished.py` and `test_worker.py` and `test_coordinator_app.py` are all being rewritten (not just updated), so "existing tests" is a misleading phrase. Should read: "all tests in the unchanged test files pass; rewritten test files have 100% of the scenarios listed in the plan passing."

- Missing AC: "The coordinator does not crash or produce duplicate history entries when `/finished` is called twice in rapid succession with the same `session_id`/`workflow_id`." This is the idempotency guarantee and should be an explicit AC.

### Questions Review (Q1–Q6)

**Q1 (stale `/finished` with no `current_task`): Option B is correct.**
The single-worker constraint makes Option B safe. The only realistic scenario for a stale call with `current_task is None` is a worker retry arriving after the coordinator already processed the original. In that case, Option B dispatches fresh work, which is what the now-retrying worker should do. Recommended answer is correct.

**Q2 (retry policy): Correct.**
Hardcoding 2 attempts / 1-second backoff as module-level constants in `worker.py` is the right balance. Removing configurable parameters from `run_worker_loop` is the right simplification.

**Q3 (abandoned cleanup before stop check): Correct.**
Always cleaning up the abandoned `current_task` before checking stop conditions ensures `state.json` never has a stale `current_task` in a terminal state. The overhead is negligible. Recommended answer is correct.

**Q4 (keep `session_id` in `HistoryEntry`): Correct.**
Removing it would break auditability when reading archives that span multiple sessions. Keeping it costs nothing.

**Q5 (remove `assignment_id` from `_render_prompt`): Correct.**
`Session ID`, `Iteration`, and `Workflow ID` fully identify the execution context. The agent is told exactly where to write its output files. No gap is created by removing the assignment ID line.

**Q6 (no new CLI parameters): Correct.**
The CLI `worker` command already only exposes `--coordinator`. Removing the internal retry/poll parameters from `run_worker_loop` signature is the right call.

### Deployment / Backwards Compatibility

The HTTP API is breaking. The endpoints change from:
- `POST /workers/register`
- `POST /workers/{worker_id}/next`
- `POST /workers/{worker_id}/finished`

To:
- `POST /register`
- `POST /finished`

And the response shapes change (no `assignment_id`, no `wait` action, `worker_id` gone). Any external worker implementations (scripts, other services calling the coordinator over HTTP) will break silently or loudly. The plan does not address this.

Concerns:
- loopy-loop is described as "repo-local automation." If the coordinator and worker are always the same binary version deployed together, breaking the HTTP contract is safe. The plan should explicitly state this assumption.
- If the coordinator is left running across a deployment (e.g., a long-running coordinator with a hot-reloaded worker), an old worker calling `/workers/register` will get a 404. The plan does not address rollout order. Recommended addition: "stop any running coordinator and worker before deploying; restart coordinator first, then worker."
- The `state.json` on disk uses field names from `LoopState`. After the refactor, `active_assignment` becomes `current_task` and `workers` is removed. An existing `state.json` with `active_assignment` set will fail to parse under the new `LoopState` model (Pydantic will reject the unknown field or fail to find `current_task`). The plan does not address migration of in-flight `state.json`. Since `--resume` is the mechanism for continuing a previous run, a run in progress at deployment time will fail on coordinator restart with `--resume`. Recommended addition: document that any in-flight run must be allowed to complete (or manually stopped) before deploying this change.

### Files Missing from Plan

The plan lists `test_goal_slug_validation.py` nowhere. Reading the glob output, this file exists. It is likely testing config-level validation that is unaffected, but it should appear in "No change needed" for completeness.

`test_harness_runner.py` is also absent from the plan. Same reasoning — likely unaffected, should be listed.

### Summary of Issues by Priority

**Must fix before execution:**
1. Document the backwards-incompatible `state.json` migration concern. Existing running sessions cannot be resumed with `--resume` after this change if `active_assignment` is in the persisted state. Either document this as a known limitation or add a migration path (e.g., tolerate both field names during a transition period using a Pydantic model alias or custom validator).
2. Add `test_register_recovers_abandoned_task_when_terminal` to the test plan (covers Q3's explicitly-resolved edge case).
3. Clarify in the AC that "does not mutate history" on mismatch also means "does not change `current_task`."

**Should fix before execution:**
4. Add `test_worker_finished_all_retries_exhausted` to the worker test plan.
5. Add `in_progress_session + current_task_is_set` arc to `test_register_stop_when_terminal` (or as a separate test).
6. Explicitly state in the plan that the deployment must stop all running coordinator+worker processes before upgrading (no rolling-upgrade compatibility).
7. Add `test_goal_slug_validation.py` and `test_harness_runner.py` to the "No change needed" list.

**Nice to have:**
8. Explicitly confirm in the plan that `CurrentTask` field names (`session_id`, `iteration`, `workflow_id`) match what `_has_invalid_control_output` and `_has_unresolvable_error_signal` use for path computation — so the implementer does not accidentally introduce a field-name mismatch.
9. Add a note in the stale-mismatch section that the response leaking the current task's details to the stale caller is intentional and acceptable given the repo-local, no-auth context.

### Overall Recommendation

REVISE with the above actions, then execute. The plan is structurally sound and the design decisions are correct. The simplification is genuine and appropriate. The issues listed above are mostly documentation and edge-case test gaps, not design flaws. The one hard blocker is the `state.json` backwards compatibility concern — it must be acknowledged explicitly so whoever deploys this knows to drain in-flight runs first.

Once the three "must fix" items are addressed, the plan is ready for implementation.
