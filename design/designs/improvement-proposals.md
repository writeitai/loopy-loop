# Design: Improvement Proposals

**Status:** Living — per-proposal statuses below (last status pass: 2026-07-13)
**Date:** 2026-07-12
**Relationship to other docs:** Companion to
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md)
(which records decisions that are *already made* and should not be reverted). This
doc is a tracked ledger of changes we are considering or have since shipped. It is
derived from the July 2026 review under [`../analysis/`](../analysis/), curated and
re-prioritized.

**Framing decision that drives priority:** we intend to drive a large, multi-phase
target project with the **planner/dispatcher double loop from the very beginning** —
not after a single-loop pilot. That decision moves the parent/child machinery from
"harden it later" to "harden it first," because it is exercised on day one of the
flagship use case. The ordering below reflects that.

Each proposal has: **what**, **why**, **sketch**, **effort** (S≈1–2 days, M≈1 week,
L≈multi-week), and **status**. The **Status** line is authoritative — read it first.
For proposals marked implemented, the What/Why text is preserved as the *original
problem statement*: it describes the pre-implementation state that motivated the
work, not the current behavior.

---

## Priority 0 — Foundations for the double loop

These are prerequisites for pointing the double loop at any real, multi-phase target.
Do them first.

### P0.1 — Durable session-stack / active-child recovery

**What.** Make "parent suspended on child X; child X active" a durable,
crash-recoverable fact in root state, and make coordinator startup reconstruct and
validate the session stack.

**Why.** This is the concrete gap that makes the current double loop unsafe. Today,
while a child runs, the only "active" pointer is `CoordinatorService.state_store`
aimed at the child's `state.json`; there is no durable `active_child_session_id` in
root state. After a coordinator crash, a fresh `StateStore` resolves
`latest_top_level_state_path()` → reopens the **parent**, not the running child. The
child-request file that spawned it was already deleted at dispatch, so the child is
orphaned: it may sit marked `running` forever while the parent re-dispatches new
work, and its evidence linkage is lost. **There is no test for "crash while a child
is active."** For a double loop used from day one, this is the first thing to fix.

**Sketch.**
- Persist the session stack in root state (or a small `runtime.json`): the ordered
  chain of suspended parents + the single active session id.
- A state transition atomically means "parent suspended on child X; X active."
- On startup: finalize terminal children, resume their parents; leave non-terminal
  children active. `children.json` becomes an index/projection, not the only bridge.
- Add an `attempt_id` (UUID) per dispatch so a legitimate retry is distinguishable
  from the original execution (the current key `(session_id, workflow_id, iteration)`
  cannot tell them apart).
- Write `children.json`, `result.json`, `pending_finished_request.json`, and control/
  eval artifacts atomically (temp-file + `os.replace`), as `state.json` already is.
- **Worker liveness (makes reclaim *safe*, not optimistic).** Have the `loopy worker`
  record its pid + a periodic heartbeat in the session dir. Then a second `/register`
  while a `current_task` exists can *verify* whether the worker is actually dead before
  reclaiming — closing the duplicate-work window — instead of assuming abandonment. On a
  confirmed-dead worker, apply the recovery policy to its orphaned agent processes — by
  default **bounded drain**, reap as the escape — before fresh work starts (see P2.5;
  cross-repo split in D7).
- **Salvage record for drained iterations.** A drained iteration is still re-run — its
  `result.json` never existed (the dead worker would have written it), and we deliberately do
  **not** synthesize one from the drained agents' outputs: that would fabricate an iteration
  result the coordinator never produced, exactly the false-closure trap D3 exists to prevent.
  Instead, make the salvage explicit: write `salvage.json` into the interrupted iteration's
  directory (drained agent ids, exit codes, pointers to their harness output dirs, a diffstat
  of what they changed in the working tree) and record the iteration in history as
  `abandoned_after_drain` (distinct from plain `abandoned`). The working tree preserves the
  drained agents' edits (D1 — the next iteration builds on git state); the salvage record
  preserves the *provenance*, so an auditor never meets a mystery diff, and the failure
  taxonomy (P2.3) can treat "crashed but salvaged" differently from "crashed, lost everything."
- Tests: restart mid-child; restart after child terminal but before parent resume;
  double `/register` while a task is live; late `/finished` from an old attempt.

**Scope — this recovers *state*, not *processes*.** P0.1 makes a restarted coordinator
reconstruct the session/child *state machine* from files. It deliberately does **not**
try to re-adopt any running agent processes, because that is a different problem with a
hard OS ceiling (see P2.5). This is the right split, because the two crash cases differ:
- **Coordinator crash** (what P0.1 covers): the agent CLIs are children of the *worker*,
  not the coordinator, so they are unaffected. The worker either finishes and writes its
  artifacts (recovered from files), or — if the coordinator is down at `/finished` time —
  exits *after* its agents already completed. No orphaned processes arise here; file-based
  state recovery is sufficient and correct.
- **Worker crash mid-run** (NOT covered by P0.1): the agent CLIs can orphan and keep
  running. That is a process-cleanup problem, handled separately in P2.5 — do not fold it
  into P0.1.

**Effort.** M–L. **Status.** **Implemented in 0.4.0** — `active_child_session_id`
pointer, startup session-stack walk with adoption/finalization, attempt ids,
request-file tombstones, atomic artifact writes.

### P0.2 — Keep the last-resort human escape hatch; do NOT build a preferred human-gate

**Design stance.** The goal is **full autonomy**. Human involvement is a *last
resort*, not a step in the normal flow. An earlier draft of this doc proposed a
first-class, resumable "pause and wait for a human to answer a gate" state; that is
**rejected** — it promotes human involvement from last-resort to a preferred path,
which is the opposite of the goal.

**What already exists (and is enough).** The escape hatch the autonomy model needs is
already wired end to end:
- `ControlSignal.stop_reason` is `Literal["goal_met", "unresolvable_error"]`
  (`models.py`).
- A workflow writes `control.json` with `stop_reason: "unresolvable_error"` →
  `_apply_session_control` sets the flag (`coordinator_app.py`) →
  `_apply_stop_precedence` stops the loop as terminal.
- The stock planner prompt already instructs it: *"If no useful work remains and the
  blocker is genuinely terminal, update the Session control path with stop_reason
  `unresolvable_error` and record the exact blocker in current_state.md."*

So the loop can already run unattended and, only when genuinely stuck, stop and say
"I can't finish this without a human." No new state, no `gate_request.json`, no
resume-after-human flow, no external-action approval gate.

**The only work worth doing here (small, prompt-level, low priority):**
- **Make it a genuine last resort in prompt guidance.** Ensure workflow prompts try
  hard before giving up — exhaust re-scoping, retrying with a better child goal, and
  routing around the blocker — so `unresolvable_error` fires rarely and only when
  truly unavoidable. This is prompt tuning, not code.
- **Make the give-up legible.** When it does stop, the blocker report a human reads
  should be clear and specific (the exact decision or capability that was missing, and
  what was tried). Today that lives in `current_state.md` / the control `reason`; keep
  it high-quality. A structured blocker summary is a nice-to-have, not required.
- **Optional, probably unnecessary:** distinguish a clean "gave up, a human decision
  is required" from a crash-style `failed`. Both are terminal; the single
  `unresolvable_error` reason with a good message is likely sufficient. Only split it
  if operational reports actually need to tell the two apart.

**Effort.** S (prompt guidance only) — or none. **Status.** **Resolved by decision
(D5), no code** — do not build the pause/resume gate. The autonomy escape hatch
already exists; only optional prompt/report polish remains.

### P0.3 — Per-child budgets *(withdrawn)*

**Withdrawn.** Considered and dropped. The idea was to let a `ChildSessionRequest`
carry its own `max_turns`/cost/model-profile/retry policy instead of inheriting the
parent's root config. Rejected as not important enough to justify the trouble it
brings: it expands the child-request schema and the config-resolution surface, adds
more ways for a misconfigured child to behave surprisingly, and the simpler default —
a child inherits root config, bounded by the root `max_turns` — is adequate. If a
concrete need for a smaller/different child budget actually shows up, revisit then;
until it does, keeping child config simple is the better default. (The one small,
unrelated piece of hygiene worth keeping in mind separately: give an invalid
child-request file a terminal rejection record rather than leaving it silently in the
directory — fold that into P0.1's atomic-artifact work if convenient.)

### P0.4 — Make the PM template runnable from a clean init

**What.** `loopy init --template pm_planner_dispatcher` must produce a repo that can
actually run — today it copies only `planner/` and `dispatcher/`, but the dispatcher
spawns child sessions running `inner_outer_eval`, which is **absent** from a clean
init.

**Why.** The double loop's child set is a hard dependency of the parent template.
Right now a fresh init of the PM template cannot execute a single child. The preflight
test only checks the parent, so this is invisible in CI.

**Sketch.** Either bundle the `inner_outer_eval` (or a purpose-built child) set into
the PM template, or make workflow-set dependencies declarative and installable
(`requires_workflow_sets: [...]`) with preflight validation and a clean-install
template smoke test in CI.

**Effort.** S–M. **Status.** **Implemented in 0.4.0** — the PM template ships its
child workflow set via packaged-template extra sources, with an end-to-end
clean-init smoke test.

---

## Priority 1 — Legibility and safety

### P1.1 — `events.jsonl` + usage/cost ledger, then `loopy status --watch`

**What.** Fill the reserved-but-empty `events.jsonl` with one versioned event per
significant transition, capture per-iteration token/cost/duration, aggregate into
`session.json`, and add a `max_cost_usd` stop condition. Then build a modest live
view on top (`loopy status --watch`, `loopy events --follow --json`).

**Why.** `events.jsonl` is documented as reserved but nothing writes to it. For a
long-horizon double loop the only way to see what's happening today is to tail files
by hand, and there is no record of what a session *cost*. Build the canonical event/
cost stream before any dashboard, so operations, debugging, budgets, and any future
TUI all read one truth rather than inventing separate ones. A local TUI (rich is
already a dependency) fits the solo-maintainer deployment; a web dashboard is not
justified yet.

**Sketch.** Events carry `event_id`, `schema_version`, UTC ts, root/child session ids,
task + attempt ids, a small typed payload; appended under the session lock; tolerate a
truncated final line. team-harness already records coordinator usage per turn — add a
usage adapter with explicit `known`/`unknown` fields (don't pretend worker token
costs are always measurable). Budgets enforceable at session/child/workflow/wall-clock
levels, checked before dispatch and after each result.

**Effort.** M–L. **Status.** **Implemented (unreleased)** — versioned events
appended after each committing mutation (best-effort delivery by design —
state.json stays the durable truth; torn-tail-tolerant reader); worker-read token usage from team-harness `run.json` with explicit
unknown-usage accounting; durable per-session ledger + children.json roll-up;
`model_prices`-derived cost with the `max_cost_usd` stop condition
(coordinator-side, requires prices — preflight-enforced); `loopy status`
(session stack + usage + cost, `--watch`) and `loopy events [--follow]
[--json]`. Scope notes: budget enforcement is per session subtree (a running
child enforces against its own spend until finalized — bounded by the
two-level depth limit); only session-level budgets shipped (per-workflow and
wall-clock budgets were not needed yet).

### P1.2 — Deterministic backstop under the judge (per target, for high-stakes work)

**What.** For target repos that own a trustworthy contract-test suite, add an
eval-banana **deterministic check that shells out to the repo's own commands**
(`uv run pytest`, `import-linter`, `alembic upgrade`, `make test`) as a hard gate
*underneath* the LLM judge — and have the eval runner parse `report.json` to derive
`goal_check.json` itself rather than trusting an agent to paraphrase console output.

**Why.** This is the one change that removes the single-point-of-failure noted in the
success-semantics doc, **without reverting either deliberate decision** there. It does
not reintroduce the agent-authoring problem, because the checks are repo-owned, not
agent-invented. Keep the LLM judge for the qualitative "did this achieve the
outcome"; add the deterministic suite as the objective floor so a mis-judged
`goal_met` cannot pass a red suite.

> **Boundary (from the success-semantics doc):** do **not** globally drop the stock
> `inner_outer_eval` "deterministic forbidden" rule. That rule is correct for generic
> repos where the only deterministic checks would be agent-authored. This proposal
> applies **only** to targets with a real suite, via a dedicated child workflow set
> that overrides the stock policy.

**Effort.** S–M (mostly the child workflow set + runner-side report parsing).
**Status.** **Deferred (2026-07-13)** — out of scope for the pre-target milestone.
It only pays off against a target repo that owns a trustworthy suite, so build it
as part of that target's dedicated child workflow set when the double loop is
pointed at one, not generically now. The boundary above still binds when it lands.

### P1.3 — Fix the Agent Skill and stale "multi-worker" claims

**What.** Rewrite `skills/loopy-loop/SKILL.md` (and any README lines) that describe
the removed pre-0.2.0 API, and test the skill against a clean generated repo.

**Why.** The skill still documents top-level `.loopy_loop/state.json`, "one or more
blocking workers" polling over HTTP, inline `goal`, and the removed
`.loopy_loop/workflows/<id>/` layout. v0.2.0 made state session-local, made the worker
a single ping-pong worker (not polling), and stopped loading the old workflows layout.
A skill that confidently generates the *old* setup is worse than no skill — it teaches
agents to build broken configurations. This is a pre-launch blocker. Similarly, the
README's "one or more workers" wording contradicts the single-worker model the
coordinator is explicitly safe under.

**Effort.** S. **Status.** Proposed.

---

## Priority 2 — Hardening and DX

### P2.1 — Named model/execution profiles; pin the trio; declare eval-banana

**What.** Replace hardcoded model IDs (repeated across `config.py`, `cli.py`, packaged
YAML, README, `.team-harness/config.toml`, `.eval-banana/config.toml`, and sibling
repos) with named profiles (`balanced`, `high_assurance`, `cheap`) resolved once in a
project-local file. Consider bounding the `team-harness` dependency once its contract
churns independently of loopy releases. ~~Make eval-banana a declared extra
(`loopy-loop[eval]`) or fail preflight with a clear message~~ — superseded, see Status.

**Why.** Model IDs churn (git history is full of "bump gpt-5.4 → 5.5"); duplicating a
model string across N files and sibling repos guarantees drift and a launch-day
footgun (install today, hit a decommissioned model). team-harness has already shipped
breaking pre-1.0 changes, so an unbounded `>=` dependency is a standing risk.
eval-banana was required by the recommended template without being a dependency, so a
template could require a tool that wasn't installed.

**Sketch.** One resolved profile → exact provider/model/effort, snapshotted into the
session. `loopy doctor` validates the profile against the actually-installed CLIs.
Consider a small compatibility matrix (loopy-loop × team-harness × eval-banana) tested
in CI: "treat the trio as one release train" until the contracts have survived real
use.

**Effort.** M. **Status.** **Partially resolved (2026-07-13)** — the eval-banana
piece landed the simple way: it is now a plain hard dependency (chosen over the
optional-extra design; one less install mode to document), and the worker appends
the scripts directory that actually holds the installed `eval-banana` script
(derived from the package's install record, `sysconfig` as fallback) to `PATH` so
the CLI resolves for spawned agents across supported install modes (`uv tool
install` and pipx expose only the primary package's entry points).

The named-profiles piece landed in a deliberately smaller form (2026-07-14):
**named model tiers** (`model_tiers` + `default_tier` in the root config, see
D9). Tiers cover the original drift concern — model ids declared once,
workflow prompts refer to tier names only — but scope the vocabulary to
*worker* models chosen per spawn, instead of full per-session execution
profiles (provider/API/prices), because coordinators stay uniformly strong by
policy (D9) and so never need per-session differentiation. If per-session
coordinator profiles ever become a real need, they compose with tiers rather
than replacing them. Trio pinning remains **Proposed**.

### P2.2 — `_advance()` refactor, folded into the P0.1 state-machine work

**What.** Extract the repeated "apply stop → maybe dispatch child → choose next → set
`current_task` → build run response" block (currently duplicated three times across
`register_worker()` and both branches of `finish_assignment()`) into a single
`_advance(state, cause, now) -> TaskResponse`.

**Why.** It's the one spot in `coordinator_app.py` (~706 lines) where drift will cause
a real bug, because the three copies must stay behaviorally identical. But do it *with*
P0.1, not as a cosmetic pass first — encode the new session-stack invariants and
table-driven transition tests at the same time, or the refactor just centralizes the
current ambiguity.

**Effort.** S (as part of P0.1). **Status.** **Implemented in 0.4.0** — folded into
P0.1 as `_advance()`.

### P2.3 — Richer failure taxonomy + per-workflow failure cap

**What.** Distinguish transient (retryable: coordinator 429/5xx, worker API/network)
from deterministic (a prompt that always fails) failures, each with retryability,
backoff, max attempts, and whether it consumes the workflow's turn budget. Add a
consecutive-failure circuit breaker per workflow (analogous to
`goal_check_consecutive_failures_cap`) → `stop_reason: "workflow_failure_cap"`.

**Why.** Failure currently collapses to a few strings, and a generic harness exception
becomes a failed iteration the scheduler can retry until `max_turns` — spending money
without distinguishing a transient 429 from a deterministic test failure. A wedged
`inner` shouldn't be able to burn the whole turn budget.

**Effort.** S–M. **Status.** **Implemented (unreleased)** — `failure_kind`
(`transient`/`deterministic`/`crash`/`unknown`, derived from team-harness's
structured `retryable` signal) recorded on results, `/finished`, and history;
per-workflow consecutive-failure counter in `LoopState` with coordinator-side
`workflow_consecutive_failures_cap` (default 5) stopping the loop with
`stop_reason="workflow_failure_cap"`. Retry/backoff at the loopy level was
deliberately NOT added: team-harness already retries transient API errors with
backoff (`team_harness_max_retries` etc.); a second retry layer would multiply
delays without new information.

### P2.4 — Operator experience: `doctor`, `validate`, session-aware `status`/`stop`

**What.**
- `loopy doctor`: Python/package versions, configured worker binaries + auth,
  eval-banana availability, git status, writable session dir, port availability,
  config/profile validity.
- `loopy validate`: workflow-graph referential integrity (`must_follow` /
  `run_after_successes` already checked at preflight — surface it standalone),
  template dependencies, signal schemas, required external tools — without starting a
  session.
- Make `status`/`stop` session-stack aware (today `status` can show the suspended
  parent while a child runs; `stop` sets `stop_requested` on the parent and only takes
  effect after the child terminates). Add cooperative `pause`/`resume` and a
  `stop --force` that records an outcome.

**Why.** The current CLI can silently operate the wrong session when a child is active,
and cannot interrupt a running harness except between iterations. For multi-day
double-loop runs this is a real safety and ergonomics gap.

**Effort.** M. **Status.** Proposed.

### P2.5 — Orphan agent-process cleanup on restart (team-harness level)

**What.** On a hard *worker* crash mid-run (Ctrl-C, OOM, SIGKILL, panic), the agent CLI
subprocesses (codex/claude/gemini) it spawned can orphan and keep running — still
spending money, still writing to the target checkout — with nothing tracking them.
Prevent and clean up those orphans.

**Why.** Neither loopy-loop nor team-harness persists agent PIDs. loopy-loop runs the
harness synchronously and tracks zero processes; team-harness holds the subprocess
handle only **in memory** (`AgentManager._agents`), and spawns with no
`start_new_session`/process-group, so a crashed worker's children are reparented to init
and run on unsupervised. There is no startup reaping anywhere. **Re-adopting a running
orphan is not portably possible** — process control is tied to the dead parent's asyncio
transport; a new process cannot reattach and re-drive it. So the achievable fix is
*prevent + clean up*, not adopt.

**We own team-harness, so this is a designed feature there, not a `/proc`-scraping hack
here.** The full design is written up in team-harness
`design/designs/process-lifecycle-and-reaping.md` (decision TH-D5); loopy-loop's D7 records
the ownership split. Summary of the split:
- **team-harness side (the bulk):** spawn each worker with `start_new_session=True` (own
  process group), persist `pid`/`pgid`/`starttime` into its existing per-run worker-session
  manifest, and expose a durable liveness check plus **drain / reap / ignore** policy ops
  (`reap_run(manifest, policy=..., drain_timeout_s=...)` / `th reap`) — verifying `starttime`
  so a recycled id is never killed.
- **loopy-loop side (small):** on crash recovery, pick a policy per orphan for the interrupted
  run before starting fresh. Default to **bounded drain** — let an in-flight agent finish within
  a timeout, write the salvage record (P0.1), then re-run the iteration from that completed
  state; this fits loopy's cost-conscious, git-is-truth profile (D1), avoids wasting
  near-complete API spend and half-applied edits, and the "two writers" objection is moot
  because draining happens during recovery before any new work is dispatched. Drain yields a
  finalized worker record + preserved repo edits — never a synthesized `result.json` (see the
  P0.1 salvage bullet). **Reap** (kill, then re-run) is the escape: for `stop --force` (P2.4),
  a hung orphan past the drain timeout, or a crash cause that makes finishing unsafe.
  team-harness provides the liveness check + policy ops + timeout (TH-D5); the logical-session
  resume path is a separate, larger option and is **not** proposed here.

**Effort.** M (team-harness, tracked there) + S (loopy surfacing). **Status.**
**Implemented** — team-harness 0.3.0 (TH-D5: process-group identity + `th reap`)
plus loopy-loop 0.3.0 (worker identity on `/register`, bounded-drain recovery of
orphaned agents, `salvage.json`).

---

## Explicitly deferred (not now)

- **Parallel loopy workers.** Both review passes independently argued against the
  obvious "scale = more workers" answer. Parallelism already lives inside team-harness;
  two loopy assignments editing one checkout buy nondeterministic corruption and merge
  conflicts, not throughput. Revisit only behind worktree/branch isolation with a merge
  coordinator, and only once tasks are explicitly read-only or isolated. The early work
  of a large multi-phase target is typically highly serial anyway.
- **Web dashboard.** A local TUI over `events.jsonl` (P1.1) covers the solo-maintainer
  deployment without another service + auth surface.
- **Breadth-first / arbitrary-depth child sessions.** Depth-first, one-child-at-a-time
  is the right v1 and matches a shared checkout. Harden the single-child path (P0.1)
  before adding breadth.

---

## Suggested sequencing

1. ~~**P0.1 + P2.2 together**~~ — **done (0.4.0)**: session-stack/state-machine
   hardening plus the `_advance()` refactor. (P0.2 needed no code — the autonomy
   escape hatch already exists.)
2. ~~**P0.4**~~ — **done (0.4.0)**: PM template runnable from a clean init; the
   double loop is executable end to end. (P2.5 also **done**: team-harness 0.3.0 +
   loopy-loop 0.3.0.)
3. **P1.3** — fix the Agent Skill and stale claims (now also stale against
   0.3.0/0.4.0: worker identity, attempt ids, workflow sets).
4. **P2.3** — failure taxonomy + per-workflow failure cap.
5. **P1.1** — events/cost ledger + `status --watch`, so double-loop runs are legible.
6. **P2.1 remainder, P2.4** — profiles/pins and operator UX (rolling). **P1.2** is
   deferred into the flagship target's own workflow-set design.

After (3)–(5), the double loop is ready to drive a large, multi-phase target as a
sequence of narrow work packages that runs unattended — falling back to the
`unresolvable_error` escape hatch only when a blocker is genuinely unresolvable
without a human. Deterministic backstops (P1.2) arrive with that target's own
workflow-set design. That target-execution design is the subject of a separate
doc, not this one.
