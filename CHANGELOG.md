# Changelog

## 0.11.0

- **Engine-emitted contract descriptors (#86).** The engine emits a
  machine-readable contract file every iteration (referenced from
  `paths.json`), derived directly from its own models and constants: the
  active-protocol accepted/required control fields plus a validated
  `goal_met` example, eval-receipt applicability, the evidence-reference
  grammar, and the `LayerHandoff` schema. The stock outer/planner prompts
  drop the hand-restated schema/grammar/eval prose and point at the
  descriptor, so prompt guidance can no longer drift from what the engine
  validates (the root cause of repeated rejected-terminal-control cycles).
- **Operator UX (#85).** `loopy stop --force` reaps the active iteration's
  tracked agent subprocesses so a hard stop leaves no orphans. `loopy status`
  adds a last-activity liveness line and surfaces rate-limited model families
  (from the harness run log), plus `--json`. New `loopy reload` refreshes
  workflow prompts and coordinator-operational config at the next task
  boundary without a restart — never the session-frozen goal, model, or
  workflow contracts/rosters.
- Docs: cli-reference, troubleshooting, and success-and-control updated for
  the above.

## 0.10.1

- **Grok Build opt-in.** Stock configs keep `team_harness_agents` as
  codex/claude/gemini. Commented overlays document how to enable the
  `grok` worker family when the Grok Build CLI is installed and authenticated
  (`XAI_API_KEY` or `grok login`). Requires **team-harness ≥ 0.6.1**.
- Skill and website configuration docs mention the same opt-in path.
- Dependency lower bound: `team-harness>=0.6.1`.

## 0.10.0

The session layout, IDs, and traces redesign (session-layout-and-ids.md;
principles P4/P5/P6). This changes the on-disk shape of newly created
protocol-v3 sessions; existing sessions keep their frozen layout and finish
unchanged. New minor release because the durable on-disk shape and generated
session IDs change for new runs.

- **Readable session IDs.** A new session directory is now
  `NNN_<slug>` for a root (repo-scoped ordinal, e.g.
  `001_ship-the-landing-page`) or `NN_<slug>` for a child (ordinal within its
  parent, slug from the child request id, e.g. `01_phase-0-foundations`),
  replacing the `YYYYMMDD_HHMMSS_<goalhash12>_<random8>` blob. The ordinal
  alone makes the name unique within its scope; there is no random suffix.
  Timestamp, goal hash, and a uuid are kept as machine fields in
  `session.json`. IDs are derived exactly once, at creation, and passed as
  values — never re-derived by parsing a path or re-hashing content. Legacy
  timestamp-style session directories keep loading and operating; id validation
  accepts both forms.
- **Traces folded into the session tree.** A new session writes every
  per-attempt raw artifact under `sessions/<id>/raw/<NNNN>_<workflow>/` (with
  the same `git/`, `harness/<run>/`, `protocol/`, `eval/`, `service/`
  subareas), instead of a parallel top-level `.loopy_loop/traces/` mirror. The
  attempt is identified by its iteration prefix; the attempt hash stays inside
  the artifacts. `trace_seals/`, `trace_finalization_outbox/`,
  `trace_manifest.json`, and the sealing/finalization machinery are retired for
  new sessions (they only existed to keep the mirror tree honest). An
  iteration's trace reference is now a plain session-relative path into `raw/`
  (`trace_ref.json` → `raw/<NNNN>_<workflow>`), not a `trace:<hash>` manifest
  ref. Raw writers keep atomic file writes for crash safety. Legacy sessions
  with existing `traces/` trees remain readable.
- **Self-describing receipt names.** New sessions merge the git and delivery
  receipt families into one `receipts/` directory; engine-authored git
  boundary receipts are named `receipts/<NNNN>_<workflow>_git_<phase>.json`
  (e.g. `0026_outer_git_after.json`) so `ls` output is legible without opening
  files (P4). Eval receipts keep their own already-self-describing
  `eval_receipts/` directory (see deferrals). Readers accept the legacy
  per-family directories for old sessions.
- **`raw/` is the prunable boundary.** Each new session ships a `.gitignore`
  ignoring `raw/`, the repo-level and template `.gitignore` add
  `.loopy_loop/sessions/**/raw/`, and a new
  `loopy prune-raw [--older-than DAYS] [--session ID] [--legacy-traces]`
  command deletes raw artifacts (and, with `--legacy-traces`, legacy mirror
  trees) without ever touching the durable session tree.
- **Prompt placement rule.** Both stock `preamble.txt` files now tell agents
  that scratch and verbose output go to the raw scratch dir, while anything
  another agent or human might cite as evidence — reports, audits, reviews —
  goes in the durable tree (`project_state/` or the iteration dir), never the
  prunable raw dir. The rendered header's "scratch dir" line points at the new
  raw location for new sessions.
- **Deferred (stretch, noted):** collapsing
  `harness_capability_roster.json` / `workflow_roster.json` /
  `workflow_contract.json` / `goal_contract.json` into `session.json` was not
  done — those files are hash-pinned frozen projections restored before every
  dispatch and forwarded to team-harness, so folding them would ripple across
  the frozen-state machinery. Eval receipts were likewise kept in their own
  directory rather than merged into `receipts/`, because their raw-report
  provenance binding is load-bearing v3 machinery and their filenames are
  already self-describing.

## 0.9.0

- Added child request schema v3: a single free-text `goal` brief with
  `request_id` and `origin`, and no `completion_criteria`/`stop_criteria`/
  `constraints`/`deliverables`/`required_evidence` arrays, no hashed `inputs`,
  and no `dispatch_inputs` snapshot. The child's `goal.md` and goal contract are
  the goal text verbatim; the dropped v2 arrays are treated as empty downstream.
  Schema v2 is still accepted unchanged so in-flight sessions finish.
- Put the rendered iteration prompt on a diet (single-goal-assignments.md §3).
  The header is now a fixed shape — goal, optional completion/stop criteria
  sections (omitted when empty), and a short key-paths block — followed by the
  workflow body. The ~50-path enumeration and the inlined frozen roster/
  scheduler/capability JSON are gone; the complete machine path map, rosters,
  scheduler view, and workflow contract are referenced by files through a new
  per-iteration `paths.json`. Header scaffolding (excluding goal and preamble)
  is CI-bounded to 2 KB.
- Added a shared workflow-set preamble hook: when
  `workflow_sets/<set>/preamble.txt` exists, the renderer includes it once under
  "Shared ground rules:" so per-role prompts never repeat shared rules.
- `paths.json` records `previous_worker_sessions`, the previous iteration's
  team-harness `worker_sessions.json` path (or null), enabling selective
  worker-session reuse across iterations (context-and-eval-economy A4).
- Added `run_when_requested` per-workflow scheduling: a workflow so marked is
  eligible only while `project_state/eval_request.md` exists in the session. It
  composes with the existing `must_follow`/`priority`/`enabled` gates and can
  replace `run_after_successes` for orchestrator-requested evaluation
  (context-and-eval-economy C3). `run_on_start` still unlocks the first
  scheduling pass, so a workflow can run on start and thereafter only on
  request.
- Added optional root-config `team_harness_compact_above_tokens` and
  `team_harness_prompt_cache`, carried in the wire snapshot and forwarded to the
  Team Harness factory when the installed version accepts them (ignored
  gracefully otherwise).
- Retired the eval-receipt output from the v3 stock flow. A protocol-v3
  check-runner role's frozen roster no longer advertises the `eval_receipts/`
  output; advisory evaluation is now agent-authored
  (`project_state/eval_results.md`, purely agent-owned — no engine coupling).
  The receipt-sealing/validation machinery stays intact and contract-gated on
  `check_runner_roles`, so sessions whose frozen contract still names a
  receipt-producing check-runner keep working through their lifetime, and any
  role that still emits a provenance-valid receipt still has it accepted as
  advisory evidence. Completion authority is unchanged (the durable
  orchestrator owns `goal_met`; eval never gated it — D11).

## 0.8.0

- Added the protocol-v3 orchestration contract. Each layer now has an
  inspectable semantic state spine, frozen workflow roster, attempt-local
  scheduler view, rolling handoff, and topology-neutral terminal outcome.
- Moved successful completion authority from eval roles to the declared layer
  orchestrator (`outer` or `planner`). Evaluations remain provenance-checked
  advisory evidence and may be omitted, non-passing, or cited across attempts.
- Added a frozen four-tier harness capability roster to assignments and
  coordinator prompts, including nested Team Harness coordinators and direct
  spawn audit records. Requires Team Harness 0.5.4.
- Made every protocol-v3 terminal lifecycle produce the same parent-linkable
  outcome, including engine stops without terminal control. Accepted control
  and handoff bytes are frozen in state so restart and trace refreshes cannot
  rewrite the terminal basis.
- Simplified the stock PM set to planner and dispatcher. The planner dispatches
  milestone outcomes; standalone or nested `inner_outer_eval` owns leaf
  decomposition and completion within its own scoped goal.

## 0.7.2

- Corrected the installable `loopy-loop` Agent Skill to teach the released v2
  contract instead of the retired one-level session model. Agents are now
  directed to their frozen absolute attempt and direct-spawn assignments,
  append-only user-input journal, recursive `child_requests/pending/`
  protocol, layer-scoped eval receipts and identity-bound terminal control,
  tree-wide stop behavior, and the separate gitignored state/evidence and raw
  trace planes.
- Added a regression test that requires the current recursive v2 concepts and
  rejects the obsolete one-level-child, markdown-inbox, and top-level-only-stop
  guidance. Runtime behavior and dependency floors are unchanged from 0.7.1.

## 0.7.1

- Fixed v2 eval-receipt validation to use eval-banana's canonical
  check-definition digest instead of a raw YAML file hash. Receipts now bind
  exactly the digest emitted in `report.json`, while loopy-loop independently
  recomputes it through eval-banana's public API before accepting a result.
  Requires eval-banana 0.3.5, which also preserves every check's exact judge
  prompt and collision-safe result, stream, and deterministic-evidence
  artifacts under the caller-owned attempt trace.
  An in-flight 0.7.0 attempt that authored a legacy raw-YAML digest is rejected
  once and must rerun its eval so the receipt carries the canonical digest.
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
