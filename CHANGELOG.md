# Changelog

## 0.7.1

- Fixed v2 eval-receipt validation to use eval-banana's canonical
  check-definition digest instead of a raw YAML file hash. Receipts now bind
  exactly the digest emitted in `report.json`, while loopy-loop independently
  recomputes it through eval-banana's public API before accepting a result.
  Requires eval-banana 0.3.5, which also preserves every check's exact judge
  prompt and collision-safe result, stream, and deterministic-evidence
  artifacts under the caller-owned attempt trace.
- Updated stock eval-runner guidance to copy `check_definition_sha256` from
  eval-banana's report rather than manually hashing YAML with a different
  protocol.
- Fixed the stock PM dispatcher contract so a child request hashes an
  immutable per-request selection snapshot under `project_state/dispatch_inputs/`.
  The mutable work-item ledger is updated only after request publication and
  is never itself a declared child input, preventing an otherwise inevitable
  post-attempt input-hash rejection.

## 0.7.0

- Added the recursive v2 session-layer contract: immutable scoped goals and
  workflow snapshots, typed child requests/outcomes, evidence-bound eval and
  control, absolute per-actor assignments, three-depth session execution,
  append-only user inputs, tree-wide stop/usage projections, and strict
  repository/worker capability negotiation.
- Added independently ignored, raw per-attempt traces with canonical
  team-harness run paths, spawn assignment envelopes, git boundary receipts,
  sealed manifests, inspection commands, and crash-safe finalization records.
- Updated the stock delivery and PM workflow sets so dynamic coordinators keep
  freedom over their teams while eval roles own each layer's semantic closure.
  Requires team-harness 0.5.0 and eval-banana 0.3.2.

## 0.6.0

- **Named model tiers (`model_tiers` + `default_tier`).** The root config can
  declare worker-model tiers once — tier name → agent → `{model, effort}` —
  and loopy renders the table into the harness system prompt so coordinators
  choose a tier per spawned agent (`spawn_agent(model=…, effort=…)`; the
  `effort` argument needs team-harness >= 0.4.0, now the minimum
  dependency). `default_tier` derives the
  per-agent spawn defaults (`team_harness_agent_models` /
  `team_harness_agent_reasoning_efforts`) from the named tier and rejects
  explicit duplicates, so model ids stay a one-line config edit. Tier choice
  is guidance with an audit trail, never enforcement (D8/D9); the raw tier
  declarations stay coordinator-side and never enter the wire snapshot.

## 0.5.0

- **events.jsonl is now written (P1.1).** The coordinator appends one
  versioned JSON event per significant transition — `session_started`,
  `task_dispatched`, `task_finished` (with tokens/duration/failure_kind),
  `iteration_abandoned`, `goal_check`, `child_started`, `child_finished`,
  `session_stopped` — to each session's `events.jsonl` after the producing
  state mutation commits. Delivery is best-effort by design: a crash in the
  commit-to-append window drops the event while the durable truth
  (`state.json` history/ledger) survives; readers key on `event_id` and
  tolerate gaps and a torn final line. New `loopy events [--follow]
  [--json]` tails the deepest active session's stream and follows the
  active session as it changes.
- **Usage/cost ledger + `max_cost_usd` (P1.1).** The worker reads
  coordinator-model token usage from team-harness's `run.json` and reports
  it (plus wall-clock duration) on `/finished`; the coordinator keeps a
  durable per-session ledger in `state.json` and records a finalized
  child's totals on its `children.json` record. With the new optional
  `model_prices` (USD per 1M tokens, coordinator-side), `loopy status`
  shows estimated cost and the new optional `max_cost_usd` budget stops
  the loop with `stop_reason="max_cost_usd"`. Cost explicitly covers the
  harness coordinator model only — agent-CLI subprocess spend is not
  measurable and is never pretended into the number.
- **`loopy status` shows the session stack and `--watch`.** While a child
  session runs, `status` now walks the durable parent→child pointers and
  shows the live child (previously it showed only the suspended parent);
  `--watch` re-renders every 2 seconds.

- **Failure taxonomy + per-workflow failure cap (P2.3).** Failed iterations
  now record a `failure_kind` — `transient` (provider said retry;
  team-harness's own retries were exhausted), `deterministic` (auth/config
  errors retries cannot fix), `crash` (worker died mid-iteration), or
  `unknown` — on `result.json`, `/finished`, and session history. A new
  coordinator-side `workflow_consecutive_failures_cap` (default 5) stops the
  loop with `stop_reason="workflow_failure_cap"` when the same workflow fails
  that many iterations in a row (crash-abandoned iterations included; any
  success resets the workflow's counter) instead of burning the remaining
  `max_turns` on a wedged workflow. The cap is not part of the wire config
  snapshot, so released workers are unaffected.
- **Agent Skill rewritten for the current API (P1.3).** `skills/loopy-loop/SKILL.md`
  still described the removed pre-0.2.0 surface (top-level `.loopy_loop/state.json`,
  polling multi-worker model, inline `goal`, `.loopy_loop/workflows/<id>/` layout) and
  would have taught agents to generate broken setups. It now documents the 0.4.0
  reality: `goal_file`, workflow sets, all three templates, the single
  identity-verified worker, child sessions, crash recovery, and resume semantics —
  validated against clean `loopy init` runs of every template. Remaining plural
  "workers" wording in the README corrected to the single-worker model.
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
