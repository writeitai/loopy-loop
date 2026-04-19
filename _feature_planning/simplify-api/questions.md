# Open Questions

## Q1: What should a stale `/finished` return when `current_task` is None?

**Context:** In the old design, a stale `/finished` (wrong `assignment_id`) returned the current next action, which could be `run`, `wait`, or `stop`. In the new design without `wait`, the options are `run` or `stop`. If `/finished` arrives and `current_task` is `None`, the coordinator could:

- Option A: Return `stop` with `stop_reason="stale_request"`. Clear signal that the call was unexpected. But the worker loop treats any stop as "exit", which could be wrong if the coordinator is actually ready for more work.
- Option B: Dispatch the next task immediately (act like `/register`). This keeps the worker loop going correctly if the coordinator is healthy.
- Option C: Return `stop` with `stop_reason="stale_request"` only if the state is in a stop condition; otherwise dispatch.

**Recommended answer:** Option B. A `/finished` with no `current_task` should dispatch the next task exactly as `/register` would. This handles the edge case where a very transient double-call arrives safely. Option A could incorrectly terminate a healthy worker loop.

The plan above uses Option B.

---

## Q2: Should the worker retry `/finished` on transient errors, and with what policy?

**Context:** The old worker had configurable `finished_retry_attempts` (default 3) and `finished_retry_backoff_seconds` (default 1.0). In the new design there is no lease, so a worker process that crashes between assignment and finish will be recovered by the next `/register` call's abandoned-task logic. This reduces urgency around retrying `/finished`.

**Recommended answer:** Keep a small hardcoded retry (2 attempts, 1-second backoff) within `_post_finished` for transient HTTP errors only. Remove the configurable parameters from `run_worker_loop`. This is simpler and sufficient — if two retries fail, the process exits and the next worker invocation recovers cleanly. Hardcode the values as module-level constants `_FINISHED_RETRY_ATTEMPTS = 2` and `_FINISHED_RETRY_BACKOFF_SECONDS = 1.0` in `worker.py` (not in `models.py`, since they are internal to the worker, not part of the shared model layer).

---

## Q3: Should `/register` return `stop` immediately if the state is terminal, without doing any abandoned-task cleanup?

**Context:** If `current_task` is set AND the state is terminal (e.g., someone called `loopy stop` while the worker was mid-task), should `/register` first clean up the abandoned task and then return stop, or just return stop immediately?

**Recommended answer:** Always do the abandoned-task cleanup first, then check stop conditions. This ensures the history is always consistent regardless of stop-race timing. The overhead is negligible (one history append), and it prevents a permanent abandoned `current_task` from cluttering `state.json`.

---

## Q4: Should `HistoryEntry` keep `session_id`?

**Context:** In the new design, every run in a given coordinator session shares the same `active_session_id`. `HistoryEntry.session_id` was previously useful for tracing which session a worker was assigned to. Now that `current_task` holds the session_id and all history entries within a session share the same value, it is somewhat redundant. However, it is still useful when reading `state.json` archives across multiple sessions in a single `LoopState`.

**Recommended answer:** Keep `session_id` in `HistoryEntry`. It costs nothing and preserves auditability. Remove only `assignment_id` and `worker_id`.

---

## Q5: What happens to the `_render_prompt` function — should `assignment_id` be replaced with something?

**Context:** The rendered prompt shown to the AI agent currently includes `Assignment ID: {assignment_id}`. This was useful for the agent to include in its `control.json` output (it didn't, but it could be used for correlation). Removing it leaves a small gap in context.

**Recommended answer:** Remove the `Assignment ID` line entirely. The prompt already has `Session ID`, `Iteration`, and `Workflow ID`, which are sufficient for the agent to know where to write its output files. The agent does not need a correlation ID — it is told the exact file paths (`iteration_dir`, `goal_check.json output path`).

---

## Q6: Should the `loopy worker` CLI still accept any parameters beyond `--coordinator`?

**Context:** The old `run_worker_loop` accepted `poll_interval_seconds`, `finished_retry_attempts`, and `finished_retry_backoff_seconds`. The new loop has no polling and bakes in retry policy. The CLI currently only exposes `--coordinator`.

**Recommended answer:** No new parameters needed. `run_worker_loop` drops all parameters except `repo_root` and `coordinator_url`. The CLI command stays as `loopy worker --coordinator <url>`.
