# Changelog

## 0.2.0 (breaking)

**Breaking API change — drop leases, polling, and worker identity.**

The three-endpoint API (`POST /workers/register`, `POST /workers/{worker_id}/next`,
`POST /workers/{worker_id}/finished`) is replaced by a two-endpoint ping-pong API:

- `POST /register` — returns `TaskResponse` directly (`action: "run"` or `"stop"`).
- `POST /finished` — returns `TaskResponse` for the next task or stop signal.

The `"wait"` action is gone. The worker loop is now a simple `while task.action == "run"`
loop with no polling.

**Removed models and fields:**
- `WorkerState`, `ActiveAssignment`, `RegisterWorkerResponse`, `NextActionResponse` removed.
- `assignment_id` removed from `FinishedRequest` and `HistoryEntry`.
- `worker_id` removed from `HistoryEntry`.
- `workers` dict and `active_assignment` removed from `LoopState`.
- Constants `DEFAULT_LEASE_SECONDS`, `DEFAULT_POLL_INTERVAL_SECONDS`,
  `DEFAULT_FINISHED_RETRY_ATTEMPTS`, `DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS`,
  `WAIT_ACTION` removed.

**Added:**
- `CurrentTask` model replaces `ActiveAssignment` (no `worker_id`, no lease).
- `TaskResponse` model returned by both `/register` and `/finished`.
- `LoopState.current_task: CurrentTask | None` replaces `active_assignment`.

**Crash recovery:** If a worker crashes mid-run, the next `/register` call detects the
orphaned `current_task`, records it in history as `error="abandoned"`, and dispatches
fresh work.

**Migration note:** Existing `state.json` files containing `active_assignment` or `workers`
fields will have those fields silently dropped on first read (Pydantic ignores unknown
fields). Any in-flight run at upgrade time will lose its `active_assignment` record without
a history entry. Drain all workers before deploying this version.

## 0.1.0

- Initial release.
