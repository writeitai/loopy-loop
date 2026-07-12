# Synthesis — Two independent analyses, reconciled

**Date:** 2026-07-12
**Inputs:**
[`loopy-loop-state-and-roadmap.md`](./loopy-loop-state-and-roadmap.md) (Claude / Opus 4.8)
and [`codex_parallel_analysis.md`](./codex_parallel_analysis.md) (Codex / gpt-5.6-sol, xhigh).

This is the capstone. Read it first, then the two source analyses for depth. It
records (1) what the parallel pass found that the first pass missed — **each claim
re-verified against the code**, (2) where the two agree (treat as high-confidence),
(3) where I changed my mind, and (4) one merged, prioritized action plan.

**Honest note on method:** the Codex pass was stronger than my first pass on the
foundation question because it read the actual `team-harness` and `eval-banana`
source (not just the installed wheel), **ran all four test suites**, and inspected
`ugm`'s roadmap for internal inconsistencies. It surfaced concrete, in-code
defects my pass reasoned around instead of opening. I've verified its headline
findings myself rather than take them on faith; results below.

---

## 1. Concrete findings the parallel pass surfaced — all verified

Each of these I checked against the code today. All confirmed.

### 1.1 "False closure" is DELIBERATE, not a bug — reclassified ⚠️ CORRECTED
Both analyses initially read `harness_runner.py:190` (`_normalize_harness_result()`
returns `success=True` for any `TeamHarnessResult` that didn't throw; `result.agents`
discarded) as a defect. **The maintainer confirms it is intentional, and git
confirms it: `success=True` has been unconditional since the first commit of the
file (`a4cca5e`, Apr 19) — it was never a regression.**

The coherent design: `IterationResult.success` means *"the assignment ran to
completion without the harness itself erroring"* (transport/config/exception →
`success=False`), **not** "the work was good." Semantic success is deliberately
delegated to the workflow-written artifacts (`control.json`, `goal_check.json`),
per the README. Inferring real success from `result.agents` exit codes is
unreliable — team-harness's coordinator can legitimately finish *after* a worker
fails — so the success decision was pushed to an explicit eval layer instead. Same
instinct as the LLM-judge decision (§1.2).

**Residual risk (narrow):** the scheduler keys cadence (`run_every`, `must_follow`,
`run_after_successes`) off `entry.success`, so a run where a worker actually died
still advances those counters. Minor scheduling inaccuracy, bounded by
`control.json`/`goal_check.json` being the true gates. **Optional fix:** let cadence
require an *accepted* eval, not just a completed run. Not a rearchitecture.

**The real consequence** is that the entire correctness burden rests on the eval
layer being run and honest — and that layer is LLM-as-judge by deliberate choice.
That concentration, not a harness-runner bug, is the thing to reason about (§3.1).

### 1.2 The stock eval template forbids deterministic checks — ON PURPOSE ⚠️ CORRECTED
`templates/.../eval_reviewer/prompt.txt:54,56`: *"Only create harness_judge
checks"* and *"Deterministic checks are forbidden."* Both analyses flagged this as
backwards. **The maintainer explains why it's deliberate: when agents were allowed
to *author* deterministic checks, they invented nonsense — brittle, wrong-target,
gameable checks. LLM-as-judge on a described outcome stopped agents from gaming
their own checks.** That is a valid lesson; keep the judge.

**The key distinction the blanket rule misses** — the failure mode was
*agent-authored* checks, not deterministic checks per se:
- *Agent invents a check* → garbage (what burned us; avoid).
- *Run a check the repo already owns* (`uv run pytest`, `import-linter`,
  `alembic upgrade`, `make test`, exit-code pass/fail) → deterministic but NOT
  agent-invented; it's the project's own contract.

**Implication for `ugm` (important):** `ugm`'s acceptance criteria *are* pre-written
repo-owned contract tests, so you don't have to choose. Keep the LLM judge for the
qualitative "did this achieve the described outcome," and *also* gate on `ugm`'s own
suite via an eval-banana **deterministic check that shells out to the existing
command** (not an agent-authored one). That gives a deterministic backstop under the
judge — a mis-judged `goal_met` can't pass a red suite — without reintroducing the
agent-authoring problem. The stock "deterministic forbidden" rule is right for
generic repos and wrong for `ugm`; the `ugm_wp` child set should override it.

### 1.3 `pm_planner_dispatcher` does not bundle its child workflow set ✅ VERIFIED
`loopy init --template pm_planner_dispatcher` copies only `planner/` and
`dispatcher/` (confirmed: those are the only two workflow dirs in the template).
But the dispatcher's whole job is to spawn child sessions running
`inner_outer_eval`, which is **absent** from a clean init. The PM template is not
executable as scaffolded. Directly blocks the `ugm` plan. **Fix:** bundle the child
set, or make workflow-set dependencies declarative + installable, and extend the
preflight test to cover the child. Codex §Q1.5.

### 1.4 The Agent Skill teaches a removed API ✅ VERIFIED
`skills/loopy-loop/SKILL.md` still documents top-level `.loopy_loop/state.json`,
"one or more blocking workers poll it," inline `goal`, and the
`.loopy_loop/workflows/<id>/` layout — all of which v0.2.0 removed (state is now
session-local; workers are single, ping-pong not polling; the README explicitly
says the old workflows layout "is not loaded"). A skill that confidently generates
the *old* setup is worse than no skill. **Pre-launch blocker.** Codex §Q3.

### 1.5 Active-child crash recovery can lose the child ✅ VERIFIED (by reading)
While a child runs, the "active" pointer is only
`CoordinatorService.state_store` aiming at the child's `state.json`; there is no
durable `active_child_session_id` in root state. After a coordinator crash, a fresh
`StateStore` picks `latest_top_level_state_path()` → reopens the **parent**, the
consumed child request is already deleted, and the child can be left `running`
while the parent dispatches new work. There is no test for "restart while a child
is active." My first pass flagged this path as "under-tested"; Codex identified the
actual failure mechanism. **Fix:** persist the session stack in root state; make
startup reconstruct and validate it. Codex §Q1.1.

### 1.6 `ugm`'s own roadmap is already internally inconsistent ✅ VERIFIED (spot-checked)
`ugm` is design-first and mostly hand-maintained markdown, and it has drifted:
`phase-0-foundations.md` still marks `WP-0.1` `blocked(stack-conventions)` though
the scaffold merged to `main`; `roadmap.md` still lists the embedding-model choice
as a Phase-1 entry blocker though `questions.md`/`decisions.md` record it resolved
(D63). **An autonomous planner reading the phase files literally will either stall
on already-done work or declare things done without updating the source of truth.**
This is a first-order risk for pointing a loop at `ugm`, and neither my prose fix
(§2) nor the stock template addresses it. **Fix in `ugm`:** a machine-readable
gate/WP-status projection with a CI consistency check; the driver fails closed on
disagreement and asks for reconciliation rather than guessing which doc wins. Codex
§Q2.

**team-harness / eval-banana specifics** (Codex read the source; I worked from the
wheel — defer to its detail, §Q4). Highest-signal, worth knowing before an
autonomous run:
- team-harness: normal return ≠ success (mirrors 1.1); **premature coordinator
  finalization kills live workers after `shutdown_timeout_s`=10s** even though
  Codex tasks routinely run 20–45 min; the coordinator loop is an unbounded
  `while True` with **no turn/wall-clock/spawn/token/cost cap inside one harness
  iteration** (loopy's outer `max_turns` does not bound intra-iteration cost);
  headless auto-compaction likely never fires (`_should_compact` needs the last
  message role to be `user`, but tool chains end in `tool`); `run.json` is rewritten
  without atomic replace.
- eval-banana: a **non-zero agent exit can still pass** if stdout has valid JSON
  (there's a test codifying it); the judge runs writable in the project root, so it
  **can modify what it's judging**; `pass_threshold < 1.0` lets a *critical* check
  fail as long as enough minor ones pass; some dead/stale config (`llm_max_input_chars`,
  `target_paths`).

---

## 2. Where both analyses independently agree — high confidence

Two models reasoning separately reached the same conclusions here. Treat these as
settled:

1. **Add a first-class human-in-the-loop pause state** (`paused` /
   `waiting_for_human`) with a typed gate-request artifact — do **not** encode a
   governance checkpoint as `unresolvable_error`. *(My §1.1 = Codex §Q1 rank-adjacent
   + §Q2.)* Both of us call this the enabling capability for `ugm`.
2. **Keep single-worker; do not build parallel loopy workers now.** Both of us
   argue this explicitly and against the obvious "scale = more workers" answer:
   parallelism already lives inside team-harness; parallel loopy workers on one
   checkout buys nondeterministic corruption. *(My §1.7 = Codex §Q1.10.)*
3. **Implement the reserved `events.jsonl` + cost/token ledger before any
   dashboard.** *(My §1.2–1.3 = Codex §Q1.3.)*
4. **De-duplicate the three coordinator mutator blocks into one `_advance()`** —
   but fold it into the state-machine hardening, not as a cosmetic refactor.
   *(My §1.5 = Codex §Q1.7.)*
5. **Stop hardcoding model IDs; use named execution profiles; pin team-harness to
   `>=0.2.10,<0.3`; treat eval-banana as a declared extra.** *(My §1.6 = Codex §Q1.6.)*
6. **Judge is evidence, never the sole stop gate; judge with a different model
   family than you implement with** (`ugm` D53). *(My §4 = Codex §Q4.)*
7. **`ugm` via a PM/dispatcher shape is directionally right, but only as narrow,
   human-gated, deterministic-acceptance pilots — never "build ugm" unattended.**
   *(My §2 = Codex §Q2.)*
8. **Launch = docs (tutorial with real output, architecture, workflow-authoring,
   troubleshooting, cost/security) + a demo repo + dogfood case studies; call it
   Beta not 1.0; 1.0 only after something real was built with it.** *(My §3 = Codex
   §Q3.)*

---

## 3. Where I changed my mind

### 3.1 The #1 risk is a single un-backstopped judgment, not a harness bug
My first pass named *concentration*; the parallel pass named *false closure* and I
briefly adopted it. After the maintainer's clarification (§1.1/§1.2) the accurate
framing is neither: the harness-runner behavior is deliberate, and LLM-as-judge is a
considered choice made because agent-authored deterministic checks failed. The real
#1 risk is what those two deliberate choices *combine* into: **acceptance for a
whole iteration rests on a single LLM judgment with no deterministic backstop.** For
low-stakes goals that's a fine, conscious tradeoff. For high-stakes work (`ugm`)
it's the thing to fix — not by reverting either decision, but by adding a
deterministic gate *under* the judge using the repo's **own** contract tests (§1.2).
That preserves both maintainer decisions and removes the single point of failure.
Maintainer concentration remains the #2 (slow, org-level) risk.

### 3.2 The pilot should be one work package (WP-0.4), not "Phase 0 + Phase 1"
I recommended piloting Phase 0 plus the Phase 1 walking skeleton. Codex's narrower
call is better: **reconcile `WP-0.1` against the already-merged scaffold, then pilot
`WP-0.4` (ports + import-linter contracts) only** — it's bounded, needs no billable
infra and no golden labels, and has a crisp deterministic acceptance ("an illegal
import fails CI"). Save the Phase-1 walking skeleton for the *second* pilot, once
Phase-0 infra and the extractor model seat exist, so failures are attributable. I'm
adopting WP-0.4 as the pilot.

### 3.3 The child needs its own budgets — a concrete blocker I under-weighted
Both passes note child sessions inherit root config, but Codex makes the
consequence sharp: a PM parent **cannot** give a child a small `max_turns`/cost/model
profile — the child would inherit `max_turns: 120`. For `ugm` that's a real blocker,
not a nicety. Per-child budgets move up my list (into Tier 1).

*(Where we still differ in emphasis: I lean slightly more toward shipping
observability early as the thing that makes the pilot legible; Codex leans toward
state-machine/outcome correctness first. These aren't in conflict — correctness
first, then observe — and the merged plan below sequences them that way.)*

---

## 4. Merged, prioritized action plan

### loopy-loop — do in this order

**P0 — correctness & trust (before any autonomous `ugm` run):**
1. **Durable session-stack / active-child recovery** + attempt IDs; a live
   `/register` while a task exists returns busy/409 instead of abandoning it.
   *(fixes §1.5 — the untested double-loop gap)*
2. **`paused` / `waiting_for_human` state + typed `gate_request.json` +
   `external_action_request`** for billable/destructive ops. *(§2 enabler)*
3. **Per-child budgets** (turns, wall-clock, cost, model profile) in
   `ChildSessionRequest` — today a child inherits `max_turns: 120`. *(fixes §3.3)*
4. **Deterministic backstop under the judge, for `ugm`** — an `ugm_wp` eval that
   shells out to the repo's OWN suite (`pytest`/`import-linter`/`alembic`),
   deterministic but not agent-authored, so a mis-judged `goal_met` can't pass a red
   suite. Keep the LLM judge; do NOT globally drop the "deterministic forbidden"
   rule (§1.2). *(This is the fix that removes the §3.1 single-point-of-failure.)*

**P1 — legibility & safety:**
5. **`events.jsonl` + usage/cost ledger**, then `loopy status --watch` reading it.
6. **Fix the PM template**: bundle/declare the `inner_outer_eval` child set; extend
   preflight to the child (the double loop can't run from a clean init today).
   *(fixes §1.3)*
7. **Rewrite + test the Agent Skill** against a clean generated repo; remove
   "one or more workers" claims from README/skill. *(fixes §1.4)*
8. *(optional, low priority)* Let scheduler cadence require an *accepted* eval, not
   just a completed run — closes the narrow §1.1 residual. Not a rearchitecture.

**P2 — hardening & DX:**
9. Named model profiles; pin `team-harness<0.3`; eval-banana as `[eval]` extra.
10. `_advance()` refactor folded into the P0 state-machine work.
11. Failure taxonomy + per-workflow failure cap + retryability/backoff classes.
12. `loopy doctor` / `validate` / session-aware `stop --force`; clean-install
    template smoke test in CI.
13. Fault-injection tests (restart-with-active-child, double-register, partial
    JSON at every handoff, schema migration, fake-CLI subprocess integration).

**Explicitly deferred:** parallel loopy workers; web dashboard; breadth-first child
sessions.

### `ugm` — prerequisites before pointing a loop at it
- Reconcile the roadmap: close `WP-0.1`, correct the resolved gates, and add a
  **machine-readable gate/WP-status projection with a CI consistency check** (the
  driver fails closed on markdown/git disagreement). *(fixes §1.6)*
- Pre-fill the owner "stack conventions" slot (uv, ruff, strict pyright, GH Actions,
  secrets) so Phase 0 isn't self-blocked.
- Custom `ugm_phase_driver` (parent: `gate_reconciler` → `wp_selector` →
  `dispatcher` → `evidence_reviewer`) and a small `ugm_wp` child set
  (`plan`/`implement`/`contract_eval` with `emits_goal_check`/`review`) — not the
  stock 120/160-turn loop. Only `evidence_reviewer` may change WP status.
- One goal file per WP (never "implement ugm"); exact `uv`/`make` acceptance
  commands per WP; local disposable Postgres/MinIO; human inboxes for decisions and
  external actions; a one-child-owns-the-checkout branch policy.
- **Pilot = reconcile WP-0.1, then WP-0.4**, contract-eval after every implement
  success, child capped ~8–12 turns (needs P0.4 first).

---

## 5. Bottom line

Both passes agree the bet is sound *in its restrained form* — fresh agent contexts
+ durable repo state + git + independently verified artifacts is a credible way to
push useful coding work past one chat. The correction I'm accepting from the
parallel pass is the framing of the central risk: **it is false closure, not
concentration.** The highest-leverage next move is therefore not a feature — it's
(a) make completion *typed and evidence-derived* so the loop can't lie to itself,
then (b) prove it on `ugm` **WP-0.4** under a hardened, human-gated, deterministic
pilot, recording every transition, cost, and intervention. That single honest trace
is simultaneously the correctness proof, the launch artifact, and the compatibility
test for the whole trio.
