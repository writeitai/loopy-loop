# Design: Improvement Proposals

**Status:** Proposed (not yet accepted)
**Date:** 2026-07-12
**Relationship to other docs:** Companion to
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md)
(which records decisions that are *already made* and should not be reverted). This
doc is the opposite: forward-looking changes we are *considering*. It is derived
from the July 2026 review under [`../analysis/`](../analysis/), curated and
re-prioritized.

**Framing decision that drives priority:** `ugm` (Ultimate Memory) will be driven by
the **planner/dispatcher double loop from the very beginning** — not after a
single-loop pilot. That decision moves the parent/child machinery from "harden it
later" to "harden it first," because it is exercised on day one of the flagship use
case. The ordering below reflects that.

Each proposal has: **what**, **why**, **sketch**, **effort** (S≈1–2 days, M≈1 week,
L≈multi-week), and **status**. Nothing here is committed until we accept it.

---

## Priority 0 — Foundations for the double loop

These are prerequisites for pointing the double loop at anything real, including
`ugm`. Do them first.

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
- Tests: restart mid-child; restart after child terminal but before parent resume;
  double `/register` while a task is live; late `/finished` from an old attempt.

**Effort.** M–L. **Status.** Proposed — **highest priority** given the double-loop
decision.

### P0.2 — First-class human-in-the-loop pause state

**What.** A non-terminal `paused` / `waiting_for_human` status, plus typed
`gate_request.json` and `external_action_request.json` artifacts the coordinator
surfaces and a human answers before `--resume`.

**Why.** A long-horizon loop repeatedly hits decisions it must not make: architecture
choices, model selection, provisioning billable infra, anything destructive. Today
the loop can only *fail* (`control.json` → `unresolvable_error`) or *guess*. Encoding
a normal governance checkpoint as an error is wrong — it makes a healthy pause look
like a failed session, and it is not cleanly resumable. `ugm` in particular has an
explicit register of decision gates; without this state the planner will stall or
hallucinate a decision.

**Sketch.**
- `LoopState.status` gains `paused`; `stop_reason`/control semantics gain
  `awaiting_human` as a **non-terminal** halt (resumable, not archived).
- A workflow writes `gate_request.json`:
  ```json
  {
    "schema_version": 1,
    "gate_id": "llm-stage-models",
    "blocks": ["WP-1.3"],
    "question": "Which extraction model/profile is approved for the pilot?",
    "options": ["..."],
    "recommendation": "...",
    "evidence_paths": ["..."],
    "requested_at": "..."
  }
  ```
- `external_action_request.json` gates billable/destructive operations (provision a
  DB, spend money, modify IAM) behind explicit human approval.
- `loopy status` shows open gates; the human answers into `updates_from_user.md` /
  `decisions`, then `--resume` continues.

**Effort.** M. **Status.** Proposed — pairs with P0.1 as the double-loop enabler.

### P0.3 — Per-child budgets

**What.** Let a `ChildSessionRequest` carry its own `max_turns`, wall-clock, cost,
model profile, and retry policy, instead of inheriting the parent's root config.

**Why.** Today a child inherits root config through `_preflight_for()`, so a PM
parent cannot give a child a small budget — a child spawned under the
`pm_planner_dispatcher` template inherits `max_turns: 120`. The request schema only
carries `workflow_set`, `goal`, `schema_version`. For a double loop that dispatches
many small units of work, unbounded child budgets are an uncapped spend and make
"one PR-sized slice per child" impossible to enforce. This is a direct blocker for
`ugm`.

**Sketch.** Extend `ChildSessionRequest` with an optional `budgets` block and an
optional `execution_profile` (see P2.1); the coordinator applies them to the child's
`config_snapshot` at creation instead of copying root. Enforce at most one
outstanding child request. Give invalid child-request files a terminal rejection
record rather than leaving them silently in the directory.

**Effort.** M. **Status.** Proposed.

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

**Effort.** S–M. **Status.** Proposed.

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

**Effort.** M–L. **Status.** Proposed.

### P1.2 — Deterministic backstop under the judge (per target, for high-stakes work)

**What.** For target repos that own a trustworthy contract-test suite (e.g. `ugm`),
add an eval-banana **deterministic check that shells out to the repo's own commands**
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
> (e.g. `ugm_wp`) that overrides the stock policy.

**Effort.** S–M (mostly the child workflow set + runner-side report parsing).
**Status.** Proposed — needed before `ugm` acceptance is trustworthy.

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
YAML, README, `.team-harness/config.toml`, `.eval-banana/config.toml`, and two sibling
repos) with named profiles (`balanced`, `high_assurance`, `cheap`) resolved once in a
project-local file. Pin `team-harness>=0.2.10,<0.3`. Make eval-banana a declared
extra (`loopy-loop[eval]`) or fail preflight with a clear message.

**Why.** Model IDs churn (git history is full of "bump gpt-5.4 → 5.5"); duplicating a
model string across N files and 3 repos guarantees drift and a launch-day footgun
(install today, hit a decommissioned model). team-harness has already shipped breaking
pre-1.0 changes, so an unbounded `>=` dependency is a standing risk. eval-banana is
required by the recommended template but is not a hard dependency, so a template can
require a tool that isn't installed.

**Sketch.** One resolved profile → exact provider/model/effort, snapshotted into the
session. `loopy doctor` validates the profile against the actually-installed CLIs.
Consider a small compatibility matrix (loopy-loop × team-harness × eval-banana) tested
in CI: "treat the trio as one release train" until the contracts have survived real
use.

**Effort.** M. **Status.** Proposed.

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

**Effort.** S (as part of P0.1). **Status.** Proposed.

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

**Effort.** S–M. **Status.** Proposed.

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

---

## Explicitly deferred (not now)

- **Parallel loopy workers.** Both review passes independently argued against the
  obvious "scale = more workers" answer. Parallelism already lives inside team-harness;
  two loopy assignments editing one checkout buy nondeterministic corruption and merge
  conflicts, not throughput. Revisit only behind worktree/branch isolation with a merge
  coordinator, and only once tasks are explicitly read-only or isolated. `ugm`'s early
  work is highly serial anyway.
- **Web dashboard.** A local TUI over `events.jsonl` (P1.1) covers the solo-maintainer
  deployment without another service + auth surface.
- **Breadth-first / arbitrary-depth child sessions.** Depth-first, one-child-at-a-time
  is the right v1 and matches a shared checkout. Enrich the child *contract* (P0.3)
  before adding breadth.

---

## Suggested sequencing

1. **P0.1 + P0.2 + P2.2 together** — the session-stack/state-machine hardening,
   the pause state, and the `_advance()` refactor are one coherent piece of work.
2. **P0.3 + P0.4** — child budgets and a runnable PM template; the double loop is now
   safe and executable end to end.
3. **P1.1** — events/cost ledger + `status --watch`, so double-loop runs are legible.
4. **P1.2 + P1.3** — deterministic backstop for high-stakes targets; fix the skill.
5. **P2.1, P2.3, P2.4** — profiles/pins, failure taxonomy, operator UX (rolling).

After (1)–(3), the double loop is ready to drive `ugm` as a sequence of narrow,
human-gated, deterministically-backstopped work packages — which is the subject of a
separate `ugm`-execution design, not this doc.
