# loopy-loop — State, Roadmap, and the ugm Question

**Author:** Claude (Opus 4.8), as co-maintainer
**Date:** 2026-07-12
**Scope:** Future improvements · fitness for building `ugm` · documentation & launch · the state of `team-harness` and `eval-banana`

> A second, independent analysis was run in parallel by Codex (gpt-5.6-sol, xhigh
> reasoning) and lives beside this file at
> [`codex_parallel_analysis.md`](./codex_parallel_analysis.md). Read both; where
> they disagree, that disagreement is signal.
>
> **⚠️ Read [`synthesis.md`](./synthesis.md) first.** The parallel pass verified
> several concrete in-code defects this doc reasoned around, and corrected two of
> my claims. Specifically: (1) the biggest risk is **false closure**
> (`harness_runner.py:190` marks every non-raising harness run `success=True`), not
> maintainer concentration as I state below; and (2) §4 below praises eval-banana
> for mixing deterministic + judge checks — true of the *framework*, but the
> shipped `inner_outer_eval` template **forbids deterministic checks**
> (`eval_reviewer/prompt.txt:56`), which inverts the point. The synthesis reconciles
> both passes into one action plan; this doc is my independent first pass, kept as a
> record.

---

## Executive summary

loopy-loop is a genuinely well-built alpha: durable file-based state, a pure
scheduler, atomic writes under a file lock, schema-versioned signal files,
explicit crash recovery, 116 fast tests, clean CI. The code is not the problem.
The gaps are **operational** (no live observability, thin failure taxonomy, no
cost/token accounting) and **strategic** (single maintainer, dormant since late
May, single-worker ceiling, and a dependency surface — `team-harness`,
`eval-banana`, hardcoded model IDs — that churns).

On the headline question: **loopy-loop is architecturally a strong fit for
driving `ugm`, but only for the code-and-contract-test slices of the work.** `ugm`
is a design-first, 8-phase system whose own roadmap is explicitly gated on human
decisions and measured golden-set spikes, and whose Phase 0 needs real infra
(Postgres, queues, object store). A headless agent loop can implement work
packages and pass deterministic contract tests; it cannot make the embedding-model
decision, stand up Hetzner Postgres, or manufacture a labeled golden set. The
`pm_planner_dispatcher` template is the right shape, but loopy-loop is **missing a
first-class human-in-the-loop pause state**, and that is the single most important
thing to add before pointing it at `ugm`.

---

## 1. Future improvements (ranked)

Ranked by leverage-per-effort. "Effort" is rough: S = a day or two, M = a week,
L = multi-week.

### Tier 1 — do these before any serious autonomous run

**1.1 First-class human-in-the-loop pause state. (M) — highest leverage**
Today a workflow that hits a decision it can't make has two options: fail the
loop (`control.json` → `unresolvable_error`) or guess. The planner prompt says
"mark blocked" but the loop has no `paused_awaiting_human` status — a blocked
item either stalls silently or stops the whole session. Add a real terminal-ish
state: `status: "paused"`, `stop_reason: "awaiting_human"`, with a structured
`questions_for_human.md` / `pending_decisions.json` the coordinator surfaces via
`loopy status`, plus a clean `--resume` that re-reads `updates_from_user.md` and
continues. This is the gating capability for `ugm` (see §2) and useful for every
non-trivial goal.

**1.2 Live observability: `events.jsonl` + `loopy watch`. (M)**
`events.jsonl` is documented as "reserved" but not written. Right now the only
way to see what a running loop is doing is to tail files by hand. Implement the
append-only event stream (iteration start/finish, workflow chosen, stop-reason,
control/goal-check transitions, child spawn/return) and a `loopy watch` TUI
(rich already a dependency) that renders the session timeline, current task,
history, and last result. For long-horizon runs this is the difference between
"trust the loop" and "babysit the loop."

**1.3 Cost & token accounting. (S–M)**
There is no record of what a session *cost*. team-harness has a `tracking/`
module — surface its per-iteration token/cost data into the iteration directory
and aggregate it into `session.json`. Add a `max_cost_usd` stop condition
alongside `max_turns`. Without this, an autonomous multi-day `ugm` run is an
uncapped spend.

### Tier 2 — robustness and clarity

**1.4 Richer failure taxonomy. (S)**
Failure currently collapses to `abandoned`, `invalid_goal_check_output`,
`invalid_control_output`, `goal_check_broken`, `no_eligible_workflow`,
`unresolvable_error`. Missing: distinguishing *transient* worker/API failure
(retryable) from *deterministic* workflow failure (a prompt that always fails),
and a consecutive-failure circuit breaker per workflow (analogous to
`goal_check_consecutive_failures_cap`) so a wedged `inner` doesn't burn all
`max_turns`. Add `stop_reason: "workflow_failure_cap"`.

**1.5 De-duplicate the coordinator mutators. (S)**
`coordinator_app.py` repeats the "check stop → maybe dispatch child → choose next
→ set current_task → build run response" sequence three times (`register_worker`,
the `finish_assignment` no-current-task branch, and the matched branch). Extract a
single `_advance(state, now) -> TaskResponse` helper. It's ~706 lines today; this
is the one spot where drift will cause a real bug, because the three copies must
stay behaviorally identical.

**1.6 Config: stop hardcoding model IDs in templates. (S)**
Templates pin `gpt-5.5`, `claude-opus-4-8`, `gemini-3.5-flash`; git history is
already full of "bump gpt-5.4 → 5.5" commits. Introduce a `models:` alias block
(e.g. `strong`, `cheap`, `judge`) resolved once, and/or read defaults from
environment, so a model rev is one edit, not N. Document the supported IDs in one
place with a "last verified" date.

### Tier 3 — scaling and reach (only if the use case demands it)

**1.7 Parallel workers — deliberately *not* now. (L)**
v0.2.0 intentionally removed leases/worker-identity for a clean single-worker
model, and much of the recovery logic is commented "safe in the single-worker
model." Re-introducing concurrency is a redesign of the state machine, not a flag.
`ugm`'s child-session model is depth-first anyway, and one honest worker per
session is easier to reason about. **Recommendation: keep single-worker; instead
invest in child-session breadth (below).**

**1.8 Child sessions: breadth and durability. (M–L)**
v1 is depth-first, one child at a time, and a child's `parent_session_id` linkage
plus `_resume_parent_if_active_child_completed` is the fragile seam. For `ugm`,
one-WP-at-a-time is actually fine (see §2), so this is lower priority than it
looks — but if PM throughput ever matters, allowing N independent children with a
join barrier is the natural next step. Add tests for parent/child crash recovery
*across* the boundary first; that path is under-tested relative to its risk.

**1.9 DX polish. (S, ongoing)**
`loopy status` should show cost, current workflow, and pending human questions;
`loopy stop` should be graceful (finish current iteration) vs `--now`. A
`loopy doctor` that validates config + workflow graph (`must_follow` /
`run_after_successes` referential integrity is checked at preflight — surface it
as a standalone command) would catch template mistakes before a run.

### Testing gaps worth closing
- Parent↔child crash recovery across the boundary (highest-risk under-tested path).
- `run_after_successes` cadence interacting with `must_follow` (combinatorial;
  the scheduler is pure — property-based tests would pay off here).
- Concurrency: there's a file lock but no test that simulates two workers racing
  `/register` (even if unsupported, it should fail safely, not corrupt state).

---

## 2. Is loopy-loop up for building `ugm`?

**Short answer: yes for the code, no for the whole thing unaided — and that's fine
if you scope it right.** Use `pm_planner_dispatcher`, but add a human-gate and
pilot on one phase.

### Why the fit is genuinely good

`ugm` is almost eerily well-matched to the pm/dispatcher model:

- Its roadmap is already **work packages (WP-N.x) that are "pointers with
  contracts": one-line goal, a minimal reading list, dependencies, a deliverable,
  and acceptance criteria drawn from the designs' own contract tests.** That is
  *exactly* the shape of a good child-session goal. You barely have to translate.
- Acceptance is tied to **deterministic contract tests** (import-linter dependency
  arrows, grain CI, envelope invariants, canaries, schema migrations). A child
  `inner_outer_eval` session can implement a WP and an `eval_runner` can *actually
  decide* pass/fail on these — no LLM judgment required for the gate.
- The whole project is **designed to be executed by coding agents** "after reading
  only the documents named in its *Reads* column." Someone already did the hard
  work of making this agent-consumable.
- Continuity-in-files matches `ugm`'s own "designs say how, plans say order,
  git is truth" philosophy.

The planner → dispatcher → child(`inner_outer_eval`) loop maps cleanly:
`planner` maintains PM state mirroring the phase file's WP status table; `dispatcher`
turns the selected WP into a child goal (goal text = WP goal + Reads list +
acceptance criteria); the child implements and self-evaluates against the contract
tests; `planner` reviews the child's evidence (PR, CI status, eval report) and
marks the WP `accepted` or `needs_rework`.

### Where it will break — be honest about these

1. **Human-decision gates.** `ugm`'s roadmap has an explicit *gate register*:
   embedding model + dimension (blocks Phase 1 entry, "hardest-to-change choice in
   the system"), LLM-per-stage, PageIndex hosted vs self-hosted, HA/observability
   choices, and an owner-provided "stack conventions" slot that **blocks Phase 0
   WP-0.1 today**. A headless loop must not guess these. Without the pause state
   from §1.1, the planner will either stall or hallucinate a decision. **This is
   the blocking gap.**

2. **Infra standup.** Phase 0 needs Postgres + Alembic migrations + queues
   (`LISTEN/NOTIFY` + `SKIP LOCKED`), Phase 1 pulls in LanceDB, later phases GCS,
   LadybugDB, PageIndex. An agent can *write* the migration and the adapter code;
   it cannot reliably provision a database, hold cloud credentials, or run a real
   `gcsfuse` mount from inside a sandboxed `codex` subprocess. Anything whose
   acceptance requires a live external service will fail the eval even when the
   code is correct.

3. **Measured spikes.** "The eval harness precedes everything tunable" — nearly
   every design defers its numbers to golden-set measurement (D17 thresholds, D35
   recall, adjudicator gates). Those WPs need labeled data, real models, and money,
   and their acceptance is a *number a human interprets*, not a green test. These
   are human-run experiments, not autonomous work packages.

4. **Cross-WP coherence / architectural drift.** The planner reviewing one WP at a
   time can lose the forest. `ugm` mitigates this with import-linter and contract
   CI (drift shows up as a failing build), which is a real safety net — but the
   planner prompt should be told to treat a full-suite CI run, not just the WP's
   own tests, as part of acceptance.

### Concrete recommendation

**Pilot, don't boil the ocean. Drive Phase 0 + the Phase 1 walking skeleton,
agent-executing only the code-and-deterministic-test WPs, with humans owning gates
and infra.** Specifically:

**A. Unblock and pre-decide before you start.**
- Fill `ugm`'s "stack conventions" slot yourself (uv, ruff, pyright strict, GitHub
  Actions, secrets handling) so Phase 0 WP-0.1 isn't `blocked(stack-conventions)`
  on turn one. It's already the house style — write it into a decision.
- For the pilot, use a **local docker-compose Postgres** and a **local MinIO** so
  the agent has real-but-disposable infra to test against; provide connection
  details in the session config / a `.env` the workers can read.

**B. Add the human-gate mechanism (§1.1).** Until it exists, give the planner an
explicit contract: *"If a WP is `blocked(<gate>)` in the roadmap and the gate is
unresolved in `decisions.md`, do NOT dispatch it. Mark it `blocked`, write the
exact decision needed to `questions_for_human.md`, and select the next unblocked
WP. If no unblocked WP remains, stop with `unresolvable_error` and a clear
summary."* Then the human answers, updates `decisions.md` + `updates_from_user.md`,
and `--resume`s.

**C. Wire the goal, don't restate it.** The session goal file should point at the
phase, not re-explain the system:
> "Implement Phase 0 of ugm per `plan/plans/phase-0-foundations.md`. Source of
> truth: the designs it references. Complete WPs in dependency order. A WP is done
> only when its acceptance criteria pass in CI (import-linter arrows, migrations
> apply cleanly, contract tests green). Do not implement WPs marked `blocked(...)`
> whose gate is unresolved in `decisions.md` — surface them for human decision."

**D. Sketch config for the pilot** (`ugm/loopy_loop_config.yaml`):
```yaml
goal_file: loopy_loop_goal.txt          # points at Phase 0, per (C)
workflow_set: pm_planner_dispatcher
max_turns: 120
goal_check_consecutive_failures_cap: 3
team_harness_provider: "codex"
team_harness_model: "gpt-5.5"           # planner/dispatcher coordinator
team_harness_agents: ["codex", "claude"]
team_harness_agent_models:
  codex: "gpt-5.5"
  claude: "claude-opus-4-8"
team_harness_agent_reasoning_efforts:
  codex: "high"
# add when §1.3 lands:
# max_cost_usd: 50
```
Child sessions run `inner_outer_eval`; each child goal = one WP with its Reads
list and acceptance criteria pasted in.

**E. Success criterion for the pilot itself:** can the loop take Phase 0 from
scaffolding to "migrations apply + import-linter passes + the harness skeleton
runs" with the human only answering gate questions and provisioning infra? If yes,
graduate to Phase 1. If the planner keeps needing rescue on non-gate work, the
problem is prompt/eval design, not the phase — fix that before scaling.

**Bottom line for §2:** loopy-loop is up for the *implementation* of `ugm`, phase
by phase, if you (1) add the human-gate pause, (2) keep infra + measured spikes in
human hands, and (3) lean on `ugm`'s already-excellent contract-test acceptance as
the eval gate. Do not point it at "build all of ugm" and walk away.

---

## 3. Documentation & official launch

loopy-loop is already on PyPI (0.2.1, Alpha) with a strong README, an HTTP
contract doc, a session-layout doc, an Agent Skill, and a real CHANGELOG. That's a
better starting position than most "launched" tools. What's missing is the
**narrative middle** between "here's the reference" and "here's how I actually use
this."

### What's missing (in priority order)

1. **An end-to-end tutorial with real expected output.** Pick one small, real
   goal (e.g. "add a `/health` endpoint with a test"), run it start to finish, and
   show the actual session directory that results — the iterations, the
   `goal_check.json`, the stop. The README explains the machinery; nobody has seen
   it *move*. This is the single highest-value doc.
2. **A concepts / architecture page.** One diagram: coordinator ↔ worker ↔
   team-harness ↔ agent CLIs, and the session-directory as the durable spine.
   Explain *why* file-state-not-chat, the two-endpoint contract, and the
   single-worker model as a deliberate choice. This is where you win over the
   skeptical reader.
3. **"Write your own workflow set."** The scheduler semantics (`priority`,
   `run_every`, `must_follow`, `run_after_successes`, `emits_goal_check`,
   `run_on_start`) are powerful and non-obvious. A cookbook — "how do I express
   'run X after every 10 successes of Y'", "how do I gate a stop on an eval" —
   turns the config reference into something people can build with.
4. **Troubleshooting / operations.** What `stop_reason`s mean and what to do about
   each; how to resume; how to read a failed iteration; how to interpret
   `goal_check_broken`; expected cost ranges. Ties directly to §1.2/§1.3.
5. **A demo/example repo.** A tiny target repo pre-initialized with a template and
   a goal that a reader can clone and `loopy coordinator` against, plus a recorded
   run. Removes all setup friction.

### Launch risks to close first

- **Model-ID churn (§1.6) is a launch-day footgun.** A new user who installs today
  and hits a decommissioned `gpt-5.5` sees a broken tool. Alias the models and
  document supported IDs with a verified date before you promote it.
- **The team-harness / eval-banana coupling is under-explained.** A launched
  loopy-loop implies "these work." Pin known-good versions, and state plainly in
  the README what auth/keys each needs (Codex login vs `OPENROUTER_API_KEY`).
- **No stability promise.** Alpha is honest today. Decide what "beta" means
  (config schema frozen? HTTP contract frozen?) and put a one-line stability policy
  in the README so early adopters know what can change under them.

### Suggested sequencing

1. **0.3 (docs + safety):** tutorial, concepts page, model-alias config,
   troubleshooting, pinned dependency versions. No API changes. → announce as a
   *soft* launch (a blog post / README badge), still labeled Alpha.
2. **0.4 (observability):** `events.jsonl` + `loopy watch` + cost tracking (§1.2/
   §1.3). This is what makes a public demo compelling.
3. **0.5 → beta:** freeze the config schema and HTTP contract, publish the
   stability policy, ship the demo repo, write the "workflow set cookbook."
   *Beta = "we won't break your config without a major bump."*
4. **1.0:** only after a real external project (ideally `ugm`, or a public pilot)
   has been driven end-to-end and you can point to it. **Don't call it 1.0 until
   something real was built with it.** That artifact *is* the launch.

The most credible launch is not a version number — it's "here is a non-trivial
system that loopy-loop built, here's the session directory, here's what it cost."
`ugm`'s Phase 0/1 pilot could be exactly that story.

---

## 4. State of team-harness and eval-banana

The user asked for detail here, because these are the foundation loopy-loop stands
on. (Codex's parallel report reads their source independently — cross-check.)

### team-harness (v0.2.10, ~8.7k LOC, Alpha) — the critical dependency

**What it is:** a model-agnostic orchestration harness where a *coordinator LLM*
spawns external worker CLIs (Codex, Gemini, Claude Code, opencode, pi, OpenHands)
as tool-use actions. Structure is sensible and modular: `coordinator/` (loop,
client, `codex_client`, auth, system_prompt, protocols), `agents/` (manager,
registry, spawner, `session_capture`, `api_error_classifier`, template), plus
`tools/`, `tracking/`, `ui/`, `skills/`. It supports both an OpenAI-compatible
coordinator and an experimental `codex` provider.

**Strengths:**
- Clear separation between the coordinator loop and the worker-CLI spawning; a
  registry/spawner pattern makes new agent CLIs pluggable.
- An explicit `api_error_classifier` and retry budget (`max_retries`, and
  loopy-loop exposes `team_harness_max_retries` / backoff) — transient-error
  handling is a first-class concern, which is exactly what a long-horizon loop
  needs.
- A `tracking/` module exists — the raw material for the cost/token accounting
  loopy-loop should surface (§1.3).
- Same house style as loopy-loop (Pydantic, typed, ruff/pyright, Apache-2.0),
  which lowers the cost of fixing it when something breaks.

**Risks / concerns:**
- **It is the single biggest external risk.** loopy-loop pins
  `team-harness>=0.2.10`, and its whole value proposition ("run assignments
  through a harness that spawns agent CLIs") is delegated here. A regression or a
  breaking change in team-harness breaks loopy-loop silently. For a serious `ugm`
  run, **pin an exact known-good version**, not `>=`.
- **The worker CLIs are themselves moving targets.** team-harness shells out to
  `codex`, `claude`, `gemini` binaries whose flags, auth, and model names change
  (loopy-loop's own Makefile shows `codex --yolo --model gpt-5.6`). The harness is
  only as stable as the least stable CLI it wraps.
- **`max_depth = 3` recursion** (coordinator agents spawning th-run agents) is
  powerful and a place where cost and confusion compound; for autonomous runs,
  understand and bound it.
- Alpha, same solo maintainer, same bus-factor as loopy-loop. The three repos rise
  and fall together.

**Verdict:** solid enough in architecture to build `ugm` on, *if* you pin versions
and treat the worker-CLI layer as the fragile edge. It is the right abstraction;
its risk is operational stability, not design.

### eval-banana (~0.3.0, actively developed) — the loop's judgment

**What it is:** a lightweight YAML eval framework. Deterministic checks plus
`harness_judge` (LLM-as-judge, run via a coding agent — `codex`/`claude`/etc.),
writing `report.json` / `report.md` with a configurable `pass_threshold`
(default 1.0 = every check must pass). Used by loopy-loop's `inner_outer_eval`
template as the mechanism that produces `goal_check.json`. Notably it is **not a
hard dependency** of loopy-loop — it's a convention the template adopts — which is
good decoupling.

**Strengths:**
- Actively versioned (0.0.2 → 0.3.0), the most alive of the three by release
  cadence.
- The mix matters: **deterministic checks for things that can be checked
  deterministically, `harness_judge` only for things that genuinely need
  judgment.** That's the correct design — and it's why `ugm` is a good fit, because
  most of `ugm`'s acceptance is deterministic contract tests that don't need the
  judge at all.
- `pass_threshold` and truncation controls are sensible operational knobs.

**Risks / concerns — especially for gating a loop's stop condition:**
- **LLM-as-judge is non-deterministic and gameable.** If `harness_judge` decides
  whether a loop *stops*, a flaky judge either stops too early (declares a goal met
  that isn't) or never (burns `max_turns`). loopy-loop already anticipates the
  second failure mode with `goal_check_consecutive_failures_cap` /
  `goal_check_broken`, which is the right instinct — but the *false-positive* case
  (judge wrongly says "done") is the dangerous one and has no guard.
- **Cost.** Every `harness_judge` check is an agent invocation. In a loop that
  evaluates every N iterations over a growing artifact, judge cost is a real line
  item — reinforcing §1.3.
- **The judge shares a model family with the implementer**, which invites
  correlated blind spots (the same model that wrote the code judging the code).
  `ugm`'s own design mandates *cross-family checkers* (D53) for exactly this
  reason — mirror that in eval config: judge with a different family than you
  implement with.

**Verdict:** fine as a *component* of the stop decision, dangerous as the *sole*
gate. The loopy-loop design already agrees — "a valid `goal_check.json` does not
stop the loop by itself; it is evidence; stopping is controlled by `control.json`."
Keep it that way: use `eval-banana` deterministic checks as hard gates, use
`harness_judge` as advisory evidence a human or the planner weighs, and never let
a single LLM judgment flip a loop to `goal_met` on high-stakes work.

---

## Cross-cutting read: is the bet sound?

These three tools plus `ugm` are a coherent bet on **autonomous long-horizon agent
coding**: durable state (loopy-loop) + multi-agent execution (team-harness) +
objective acceptance (eval-banana / contract tests), pointed at a large, already
agent-consumable design (`ugm`).

- **The bet is sound in shape.** The insight that long-horizon agent work should be
  durable, file-based, and gated on *objective* acceptance rather than a chat
  transcript is correct, and it's better-engineered here than in most attempts.
- **The single biggest risk is not the code — it's the concentration.** One
  maintainer, three interlocking alpha repos, all dormant since late May, plus a
  worker-CLI/model-ID layer that churns weekly. The system's stability is bounded
  by the least stable moving part, and there's no second person to catch drift.
- **The single highest-leverage next move** is the §1.1 human-in-the-loop pause
  state, executed as the enabling step for a **narrow `ugm` Phase 0/1 pilot**. That
  one capability unblocks the flagship use case, produces the artifact that makes
  the launch credible (§3), and stress-tests team-harness/eval-banana on real work
  — three birds. Everything else (observability, cost, dedup) is in service of
  making that pilot legible and safe.

**One-sentence bottom line:** the foundation is real and well-built; the work now
is operational hardening (pause/observe/cost) and proving it on one honest slice
of `ugm`, not adding capability for its own sake.
