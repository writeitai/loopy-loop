# Architecture Decision Log

Decisions made while building and reviewing loopy-loop, recorded with the context and
rationale that a future reader — a human implementer, or an agent with **no memory of
the conversation that produced them** — needs to understand each one cold.

Companion docs:
- `design/designs/success-semantics-and-evaluation.md` — the self-contained, detailed
  form of **D3** and **D4** (this log states the conclusion; that doc makes it complete).
- `design/designs/long-running-loop-reliability.md` — the self-contained description
  of the crash recovery, telemetry, failure containment, and model-tier mechanisms
  already implemented under several decisions below.
- `design/proposals/` — forward-looking changes we are *considering* (not decided;
  do not treat them as binding or as descriptions of current behavior).
- `design/analysis/` — the July 2026 review that produced several of these decisions
  (working notes; may be messy or superseded).
- `README.md` — the user-facing behavior these decisions implement.

Each entry states the **Decision** (the conclusion, plainly), the **Context** (what
problem it solves or why the question arose), and the **Consequences** (what follows).
A `**Refined by**` line records later decisions that modify an earlier one.

> **Several of these are deliberate choices that read like defects to someone skimming
> the code.** They are recorded here precisely so a future agent does not "fix" them by
> accident. If you are about to change behavior related to an entry below, read the
> entry — and its companion design section — first.

---

## D1. Continuity lives in files and git, not in a chat transcript

**Decision.** All durable state — loop dispatch state, the goal, each iteration's
rendered prompt and result, evidence, and the stop switch — lives as inspectable files
under `.loopy_loop/sessions/<session_id>/`, alongside the target repo's normal git
history. A session is a directory you can read, pause, resume, and audit; it is **not**
a growing chat conversation.

**Context.** The common "one agent solves a large task in one long chat" approach hides
all state inside a transcript: you cannot inspect it, resume it after a crash, diff it,
or audit why it did what it did. loopy-loop exists to replace that with durable,
external structure.

**Consequences.** Every mechanism in this log assumes files are the source of truth.
Crash recovery reads artifacts on disk (e.g. `pending_finished_request.json`,
`result.json`), never in-memory conversation. A finished run is legible after the fact.
This is the premise the rest of the log depends on.

## D2. The single-worker model is deliberate

**Decision.** The coordinator drives exactly **one** worker at a time, over a
two-endpoint ping-pong API (`POST /register`, `POST /finished`). v0.2.0 deliberately
removed leases, polling, and the multi-worker registry/scheduling model to get here.
v0.3.0 later added process identity solely to prove ownership and liveness during
crash recovery; it did not restore a worker pool. Several recovery paths in
`coordinator_app.py` are correct **only** under this single-worker assumption, and say
so in comments.

**Context.** loopy-loop coordinates stateful mutations to a single working checkout.
Multiple concurrent workers editing one checkout would need worktree/branch isolation, a
merge coordinator, and conflict recovery — a large, error-prone machine. Intra-iteration
parallelism already exists one layer down, inside `team-harness` (a coordinator LLM can
spawn several worker CLIs at once).

**Consequences.** Do **not** reintroduce parallel loopy workers as a "scaling" feature;
it is a redesign of the state machine, not a flag. The deliberate boundaries in
`designs/long-running-loop-reliability.md` record it as rejected, not pending work.
Recovery logic may rely on "at most one task is live." Child sessions are depth-first,
one at a time (see D6).

## D3. Iteration success means "the assignment ran," not "the work is good"

**Decision.** `IterationResult.success` is `True` whenever a `team-harness` run returns
normally, and `False` only when the harness itself raises. It is **not** a judgment
about whether the requested work was accomplished, and the per-worker exit codes in
`TeamHarnessResult.agents` are intentionally not consulted. Whether the work was any
*good* is decided by the evaluation layer via `control.json` (the stop switch) and
`goal_check.json` (evidence).

**Context.** `team-harness`'s coordinator is an orchestrator, not a build system: it can
legitimately return a normal result after a worker failed (synthesize an answer, route
around a dead worker). Worker exit codes are therefore a *noisy* proxy for real success
— a non-zero worker can accompany a good outcome, and all-green workers can accompany a
useless one. Mapping them to a boolean would manufacture false precision. This has been
the behavior since the first commit of `harness_runner.py`; it is original intent, not
drift.

**Consequences.** The evaluation layer, not the harness return value, is the real
arbiter of completion. The scheduler keys cadence off mechanical success, so a
harness-completed-but-worker-failed run still advances `run_every`/`must_follow`
counters — an accepted, bounded inaccuracy, because `control.json`/`goal_check.json`
remain the true gates. Full reasoning and alternatives:
`design/designs/success-semantics-and-evaluation.md` (Decision 1).

## D4. Evaluation is LLM-as-judge; agents do not author deterministic checks

**Decision.** In the packaged `inner_outer_eval` workflow set, the eval workflows create
**only** `harness_judge` (LLM-as-judge) checks that describe desired *outcomes*.
Authoring deterministic checks is explicitly forbidden in the stock template.

**Context.** This is a lesson from experience, not theory. When agents were allowed to
*author* deterministic checks, they produced brittle, wrong-target, gameable ones — the
implementer inventing its own pass/fail criteria let it game itself. Judging a described
outcome removes that failure mode. **Important boundary:** the thing that failed was
*agent-authored* checks, not deterministic checks as a category. Running a check the
repo *already owns* (`pytest`, `import-linter`, `alembic upgrade`, exit codes) is
deterministic but is not that failure mode.

**Consequences.** The stock "deterministic forbidden" rule is correct for generic target
repos, where the only deterministic checks would be agent-invented. For a target that
already owns a trustworthy contract-test suite, the right configuration is *both* — the
judge for qualitative outcomes, plus a deterministic backstop that shells out to the
repo's own suite — via a dedicated child workflow set, not by loosening the stock
template. Judge with a different model family than the implementer where practical. A
single judge pass is evidence, not a hard gate for a high-stakes stop. Full reasoning:
`design/designs/success-semantics-and-evaluation.md` (Decision 2).

## D5. Full autonomy, with `unresolvable_error` as the only, last-resort human escape hatch

**Decision.** The design goal is to run **fully autonomously, with no human in the
loop**. Human involvement is a *last resort*, never a step in the normal flow. The one
sanctioned escape hatch is already built: a workflow that hits a genuinely terminal
blocker writes `control.json` with `stop_reason: "unresolvable_error"`, which stops the
session as terminal and leaves a recorded reason. We deliberately **do not** build a
preferred, resumable "pause and wait for a human to answer" gate.

**Context.** An autonomous long-horizon loop will occasionally hit something it truly
cannot do alone — a decision only a human can make, a credential it lacks, a
billable/destructive action it is not permitted to take. It must not silently guess, and
it must not stall. But making a human-answered gate a *normal* step would defeat the
whole autonomy goal. The escape hatch already exists end to end and needs no new
machinery:
- `ControlSignal.stop_reason` is `Literal["goal_met", "unresolvable_error"]`
  (`models.py`).
- Writing it flows through `_apply_session_control` → `_apply_stop_precedence` in
  `coordinator_app.py`, which stops the loop.
- The stock planner prompt already instructs it: *"If no useful work remains and the
  blocker is genuinely terminal, update the Session control path with stop_reason
  `unresolvable_error` and record the exact blocker."*

**Consequences.** Workflows must **exhaust autonomous options first** — re-scope, retry
with a better child goal, route around the blocker — and reach for `unresolvable_error`
only when a blocker is genuinely unavoidable without a human. When they do stop, the
recorded blocker must be specific enough that a human reading it later understands
exactly what was missing and what was tried, because that report is the entire
human-facing surface. Do **not** add a `paused` / `waiting_for_human` state, a
`gate_request.json` flow, or an external-action approval gate; that alternative was
considered and rejected. This entry is the canonical disposition and the mechanism
`AGENTS.md` and `CLAUDE.md` point agents at.

## D6. Large multi-phase targets are driven by the planner/dispatcher double loop from the start

**Decision.** For a large, multi-phase target project, the intended execution shape is
the **planner/dispatcher double loop** (the `pm_planner_dispatcher` workflow set) from
day one — a parent "planner" session that maintains PM state and selects one unit of
work, a "dispatcher" that spawns a child implementation session per unit, and a review
of the child's evidence — rather than starting with a single flat loop and adding the
double loop later.

**Context.** A single `inner_outer_eval` loop pointed at "build the whole thing" drowns
in context; the double loop keeps each unit small (a fresh child context scoped to one
work package) while the parent carries durable cross-cutting state. Committing to it from
the start avoids re-architecting mid-project.

**Consequences.** The parent/child machinery is on the critical path from day one, so it
must be hardened *first* — durable active-child crash recovery and a PM template that is
runnable from a clean init are prerequisites, not later polish. Both are implemented and
described in `designs/long-running-loop-reliability.md`. Session-stack recovery
reconstructs session/child *state* from files; it does not re-adopt a crashed worker's
agent subprocesses. A hard worker crash is handled by the D7 drain/reap cleanup path.
Child sessions remain depth-first and one-at-a-time (consistent with D2). The planner
drives the target's *own* authoritative plan; it does not invent a parallel backlog.

## D7. Process-lifecycle ownership is split: team-harness owns agent processes, loopy-loop owns the worker

**Decision.** Responsibility for OS process lifecycle is split cleanly across the two repos we
own:
- **team-harness owns the agent-CLI processes** it spawns. It launches each in its own process
  group, persists their identity (pid/pgid/starttime) in its worker-session manifest, and
  provides a durable liveness check plus **drain / reap / ignore** policy operations over those
  groups. (team-harness `design/decisions.md` TH-D5;
  `design/designs/process-lifecycle-and-reaping.md`.)
- **loopy-loop owns its own `loopy worker` process.** It records the worker's hostname,
  pid, and pid-reuse-proof start-time token on the live `CurrentTask`. On crash recovery it
  (a) probes a verifiable same-host identity to tell "still running" from "dead" before
  reclaiming a task — closing the local duplicate-work window on a second `/register` — and
  (b) applies the configured policy to the interrupted run's orphaned agents. There is no
  periodic heartbeat; liveness is checked directly against the stored identity.
  **Default: bounded drain** — let an in-flight agent finish within a timeout (fits loopy's
  git-is-truth, cost-conscious profile; no concurrent-writer problem because it runs during
  recovery), with **reap** as the escape for hung-past-timeout or unsafe-to-finish work. A
  future force-stop command must reuse this cleanup path rather than inventing another one.
  A drained iteration is **never accepted or synthesized from drained outputs**: its
  `result.json` never existed, and fabricating one would trigger the false-closure trap D3
  prevents. If stop conditions still allow the session to continue, the scheduler dispatches
  another real iteration; a failure cap or `max_turns` may instead stop it. Recovery preserves
  completed repo edits through git (D1). When it processes at least one tracked harness run,
  it also writes `salvage.json` in the interrupted iteration's directory. When at least one
  orphan reaches a settled outcome, history uses `abandoned_after_<policy>` instead of plain
  `abandoned`. These records make the provenance of surviving edits auditable rather than a
  mystery diff.

**Context.** Before v0.3.0, loopy-loop ran the harness synchronously inside its worker
without persisting process identity. The agent CLIs are children of that worker, not of
loopy-loop's coordinator, so a hard worker crash could orphan processes that kept spending
money and writing to the checkout while the coordinator could not distinguish a live worker
from a dead one. Neither problem is solvable by re-adopting processes (impossible — see D6
and team-harness TH-D2). For a verifiable same-host identity, both are solved by *tracking
identity, then draining or reaping*, with the natural split "each layer owns the processes it
spawns." A remote worker remains unverifiable and falls back to legacy abandonment because
its processes cannot be reached from the coordinator host. A missing start-time token or
identity provider prevents the worker-liveness proof; same-host agent recovery is still
attempted when team-harness has usable run records, and otherwise degrades to legacy
abandonment. Those fallback paths cannot prove the old writer is gone.

**Consequences.** The process-lifecycle mechanism (liveness + drain/reap/ignore) is a
team-harness feature (we own it — it is not a `/proc`-scraping hack bolted onto loopy-loop);
loopy-loop consumes it. loopy-loop persists the worker's hostname/pid/start-time identity and
applies one configured recovery policy to the interrupted run's orphans (default bounded
drain, reap as escape). For identity-tracked same-host runs this makes D6's state recovery
verify-dead-before-reclaim rather than optimistic; legacy and remote identities retain the
documented limitation above. The implemented protocol and salvage boundary are described in
`designs/long-running-loop-reliability.md`; the process-group mechanism is team-harness TH-D5.

## D8. Constraints on agents are fail-closed detection with a repair path, never hard prevention

**Decision.** The system constrains agent behavior by **detecting** violations in evidence
and blocking *acceptance* of the work until they are repaired — never by **preventing** the
action up front. No preventive fences: no path-level write sandboxes, no semantic scheduling
vetoes ("this workflow may not run until X is proven"), no approval gates, no arbitrary
mid-run hard-fails. Every constraint must be expressed as something the agent can see,
contest, and repair against — an evaluation check or a recorded disposition — and the only
hard stops are the evaluation gates that decide whether work is *accepted*
(`goal_check.json` / `control.json`), not whether it may be *attempted*.

**Context.** Stated as a general principle by the author (July 2026, during the design-loop
work in writeit-loops-and-standards): agents should have enough freedom to decide;
"fail-closed detection, not prevention" is the correct mental approach. It generalizes what
three existing decisions already do individually: D3 keeps mid-run "success" mechanical and
moves all quality judgment into after-the-fact evidence; D4 bans *agent-authored* pass/fail
criteria while keeping repo/set-owned checks as detection backstops; D5 rejects a preferred
human gate in favor of evidence-in (`updates_from_user`) and a last-resort terminal stop.
The reasoning: prevention encodes today's guess about what agents shouldn't do and hides its
own mistakes, while detection publishes every constraint as a visible, arguable check
failure with a defined relaxation route — a wrongly-scoped check gets repaired with a
counterexample and independent review instead of being silently obeyed forever. Concrete
shape (from the design-loop): a workflow set ships a deterministic "write barrier" check
that diffs protected paths against the session-start digest; a child session can physically
write anywhere, but cannot terminate successfully while the barrier fails — fail-closed
detection, not a sandbox.

**Consequences.** New engine features and workflow sets must not introduce preventive
mechanisms: no coordinator-enforced path permissions, no eligibility gates keyed to semantic
state, no paused/waiting-for-human states (already banned by D5). Where discipline over
files is needed (a research workflow must not touch binding docs), express it as a shipped
deterministic check over the diff — consistent with D4's boundary (set-owned, not
agent-authored) — whose failure blocks the session's goal check until the write is undone.
The accepted cost: a violating action can occur and must be detected and repaired after the
fact; that inefficiency buys inspectability and reversibility of the constraint itself.

## D9. Coordinators are uniformly strong; worker model choice is per-spawn, prompt-guided, and audited — never enforced

**Decision.** Every harness coordinator in a session tree — the root PM loop, child
implementation loops, any deeper level — runs the **same strong coordinator model**
(`team_harness_model`, one value per repo). Cost control comes from the **workers**:
the root config may declare **named model tiers** (`model_tiers`: tier name → agent →
`{model, effort}`), and each coordinator chooses a tier per spawned agent via
team-harness's per-spawn `spawn_agent(model=…, effort=…)` overrides. Tier selection is
**guidance rendered into the system prompt plus an audit trail** (team-harness records
requested/effective model and effort per agent in `run.json`) — the engine never
validates or blocks a coordinator's model choice (D8). With `default_tier` set, the
named tier *derives* `team_harness_agent_models` / `team_harness_agent_reasoning_efforts`
(setting both is a config error), so a model id lives in exactly one place.

**Context.** The obvious alternative — differentiating whole sessions ("strong parent
session, cheap child session", per-session execution profiles carried on
`ChildSessionRequest`) — was analyzed (July 2026) and rejected for now, consistent with
the withdrawal of P0.3. Uniform strong coordinators dissolve that design's two hardest
problems at once: the cost ledger stays correct (loopy only meters the coordinator
model, so one repo-global `model_prices` remains valid), and a child's planning/eval
reasoning is never downgraded along with its implementation muscle (the D4 concern of a
weak session judging its own work). The coordinator context is also cheaper than it
looks: it orchestrates on bounded log tails and status polls while worker CLIs — billed
to their own accounts — chew the bulk tokens. Tier names are deliberately
capability-semantic bundles of model + effort ("strong", "economy"), not raw
model-id/effort axes, so prompts reason about one word and model churn stays a one-line
config edit (the P2.1 drift concern).

**Consequences.** Workflow prompts should name **tiers**, never model ids; the rendered
guidance block (`render_model_tier_guidance`, `config.py`) is the only place tiers
expand to models. Adherence is probabilistic by design: a coordinator can forget to
escalate a review — the remedy is the audit trail (an outer reviewer or an eval check
verifies `requested_model`/`effective_model` on the child's agent records), never an
engine fence (D8). Do not add per-session/per-depth model allowlists, "children may not
request expensive tiers" vetoes, or coordinator-model differentiation per loop level; if
per-session coordinator profiles ever become genuinely needed, they compose with tiers
(profiles set session defaults, tiers guide per-spawn choice) and require amending this
decision. Effort-as-spawn-argument lives in team-harness (0.4.0+, TH-D6); on older
installed versions the tier guidance still works for `model`, and effort escalation
falls back to raw CLI `flags`.
