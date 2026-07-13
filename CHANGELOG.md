# Changelog

## Unreleased

- **eval-banana is now a hard dependency.** The recommended
  `inner_outer_eval` template (and the PM template's child sessions) shell
  out to the `eval-banana` CLI; previously it had to be installed
  separately, so a fresh install could dispatch eval workflows against a
  missing tool. It now installs with loopy-loop — no extra, no preflight.
- **Worker appends its bundled-CLI scripts directory to `PATH`.** Harness
  agents inherit the worker's environment, but under `uv tool install` /
  `pipx` only loopy-loop's own entry points are exposed on `PATH`, so
  dependency CLIs such as `eval-banana` were invisible to agents. The
  directory is derived from where eval-banana's script was actually
  installed (its package record; `sysconfig` only as fallback — the default
  scheme is wrong for e.g. `pip install --user` under a system
  interpreter). It is appended, never prepended, and existing `PATH`
  entries are preserved verbatim: the target repo's own `python`/tooling
  and any eval-banana already on `PATH` keep winning.

## 0.4.0 (breaking)

**Breaking API interaction — completions must echo the dispatched
`attempt_id`.** Every dispatched task now carries an `attempt_id` that the
worker must echo on `/finished`; a completion without the live task's attempt
is treated as stale (this is what fences superseded work). A 0.3.0 worker
therefore cannot complete tasks against a 0.4.0 coordinator — upgrade workers
and coordinator together (they normally ship in the same install).

- **Durable session-stack recovery (P0.1).** While a child session runs, the
  parent's `state.json` records `active_child_session_id`; on `--resume` the
  coordinator walks the pointer chain to the deepest live session instead of
  silently reopening the parent and orphaning the running child. Terminal
  children found at startup are finalized (children.json completed, pointer
  cleared) and their parent resumed. Every interrupted-dispatch crash window
  reconciles deterministically: dangling pointers are cleared, a fully
  created child whose parent commit never landed is adopted, and leftover
  request files never dispatch twice (children.json records the originating
  `request_file`). Invalid child requests are terminally rejected
  (`*.json.rejected`) instead of being re-read forever.
- **Attempt ids.** Every dispatched task carries a unique `attempt_id`
  (also on the wire in `TaskResponse`, echoed in `FinishedRequest`); a late
  `/finished` from a superseded attempt of the same coordinates is treated as
  stale rather than recorded as the current result.
- Iteration artifacts (`result.json`, `result_text.txt`, `prompt.txt`,
  `harness_run_id.txt`, `pending_finished_request.json`), `children.json`,
  and `salvage.json` are all written atomically (unique temp + rename) — a
  crash can never leave a truncated recovery artifact.
- Internal: the three duplicated dispatch blocks in the coordinator collapsed
  into one `_advance()` step (stop checks → child dispatch → next workflow →
  stamped task), so they can no longer drift apart. `_advance()` also enforces
  the suspended-parent invariant: a parent with a live child can never acquire
  its own task (a duplicate `/finished` retry gets the child's live task
  instead), and a coordinator-level transition lock serializes cross-store
  handoffs.
- Review hardening (adversarial Codex review of the above): request-file
  tombstones apply only to RUNNING child records (a completed child's
  filename is reusable for new work); the children.json record lands BEFORE
  the child state so an interrupted dispatch is always discoverable
  (`failed_dispatch` + exactly-once redispatch); startup reconciles every
  running-projected record (terminal children finalize even without a
  pointer); the first child task carries an attempt id; attempt checks are
  strict whenever the live task has one (including `result.json` provenance —
  a stale artifact can no longer complete a new attempt); semantically
  unusable child requests (unknown workflow set, no eligible workflow) are
  terminally rejected instead of wedging every completion; packaged prompts
  instruct atomic control/goal_check publication; the crash model (process
  crash, no fsync) is documented.
- **The `pm_planner_dispatcher` template is executable from a clean init
  (P0.4).** `loopy init --template pm_planner_dispatcher` now also ships the
  `inner_outer_eval` child workflow set its dispatcher spawns — previously a
  clean init could not execute a single child session. The child set is
  sourced from the `inner_outer_eval` template itself, so the two copies can
  never drift apart.

## 0.3.0 (breaking)

**Breaking API change — `/register` requires the worker's process identity.**
A register without a `worker` object is rejected with HTTP 400. Pre-0.3
workers cannot register against a 0.3 coordinator; upgrade workers and
coordinator together (they normally ship in the same install).

- **Worker liveness verification (D7).** The worker sends its process
  identity (hostname + pid + a pid-reuse-proof start-time token) with
  `/register` and `/finished`; the coordinator stamps it onto the dispatched
  task. A `/register` while the recorded worker is *verifiably still alive*
  returns HTTP 409 instead of abandoning live work — closing the
  duplicate-work window. Unverifiable identities (remote hosts, or a
  team-harness without process identity) keep the pre-existing
  assume-abandoned recovery behavior. Because every dispatched task now has
  a recorded owner, a stale `/finished` is replayed **only to that owner**
  (anyone else gets HTTP 409) — a task persisted by a pre-identity version
  keeps the legacy replay for that one resume.
- **Orphaned-agent recovery (P2.5 / TH-D5 consumer side).** When a worker is
  confirmed dead with nothing recoverable, the coordinator applies
  `recovery_policy` (new coordinator-side config, NOT part of the wire
  snapshot; default `drain`, one shared `recovery_drain_timeout_s` deadline,
  default 600s) to agent processes the dead worker's harness run left behind,
  via team-harness's process reaper: drained agents finish and their repo
  edits survive; a `salvage.json` in the interrupted iteration directory
  records what was handled; the history entry is `abandoned_after_<policy>`
  when anything settled. Recovery runs OUTSIDE the state lock (`loopy
  status`/`stop` stay usable while draining), validates that each discovered
  run record actually belongs to the iteration, is same-host-only (a worker
  identity from another hostname skips reaping), and refuses to dispatch
  replacement work (HTTP 409) when any orphan may still be running or when
  team-harness's parent-liveness guard reports the run's owner alive.
  Requires team-harness with the process reaper (> 0.2.10); older versions
  skip orphan recovery gracefully.
- A stale `/finished` from a **different identified worker** now gets HTTP 409
  instead of a second copy of the live task; unknown identities keep the
  pre-existing stale-retry behavior. State-lock contention surfaces as a clean
  HTTP 503 (and friendly CLI errors) instead of raw tracebacks.
- The bundled worker uses an unbounded read timeout on `/register` only
  (recovery can legitimately block registration up to the drain deadline),
  keeps the bounded timeout on `/finished`, and exits with code 3 on a 409.

## 0.2.1

- Improve README onboarding, install, initialization, configuration, and logging docs.
- Update package and skill descriptions to avoid unclear repository-scope jargon.

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
