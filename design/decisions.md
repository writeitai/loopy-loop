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
- `design/designs/recursive-loop-layer-contract.md` — the implemented v2 recursive,
  provenance, delegation, and state/trace baseline plus its accepted v3 amendment
  boundary (**D10–D12**).
- `design/designs/orchestrator-owned-completion-and-cross-harness-review.md` — the
  accepted v3 contract for orchestration ownership, semantic state/handoff, optional
  evaluation, schedule/capability context, phase-sized PM dispatch, and cross-harness
  review. It is implemented in loopy-loop 0.8.0 and team-harness 0.5.4.
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

**Refined by D12.** Files remain the source of truth, but correctness-critical
session state/evidence and exhaustive execution traces have different retention
contracts. Exact loopy assignments and normalized results remain with the session;
bulk multi-agent execution detail lives in a separately ignored trace plane whose
loss cannot break recovery or acceptance.

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
`TeamHarnessResult.agents` are intentionally not consulted. Whether the session has
enough evidence to stop is decided by the durable layer's declared orchestration role
through `control.json`. Reviews, tests, child outcomes, delivery facts, and optional
evaluation results are evidence for that decision; none is inferred from the harness
return value.

**Context.** `team-harness`'s coordinator is an orchestrator, not a build system: it can
legitimately return a normal result after a worker failed (synthesize an answer, route
around a dead worker). Worker exit codes are therefore a *noisy* proxy for real success
— a non-zero worker can accompany a good outcome, and all-green workers can accompany a
useless one. Mapping them to a boolean would manufacture false precision. This has been
the behavior since the first commit of `harness_runner.py`; it is original intent, not
drift.

**Consequences.** The declared orchestration role, not the harness return value or a
scheduled evaluator, is the semantic arbiter of completion. The scheduler keys cadence
off mechanical success, so a harness-completed-but-worker-failed run still advances
`run_every`/`must_follow` counters. An absent, failing, or malformed advisory eval is
recorded as evidence/diagnostics; it does not retroactively flip mechanical success,
increment the workflow's harness-failure counter, or disable the orchestration role's
ability to decide. `control.json` remains the explicit stop switch. Full reasoning and
alternatives:
`design/designs/success-semantics-and-evaluation.md` (Decision 1).

## D4. When evaluation is used, stock checks are LLM-as-judge; agents do not author deterministic checks

**Decision.** In the packaged `inner_outer_eval` workflow set, the eval workflows create
**only** `harness_judge` (LLM-as-judge) checks that describe desired *outcomes*.
Authoring deterministic checks is explicitly forbidden in the stock template. This
decision governs the form and trust boundary of evaluation **when an orchestrator uses
it**; it does not require an eval run and does not give an eval role terminal authority.

Designing non-trivial eval checks is itself consequential reasoning. The eval-check
authoring prompt should therefore prefer parallel, independent coverage and failure-mode
analyses from different enabled harness families, followed by review of the proposed
checks by a family other than the primary author when practical. The accountable
coordinator synthesizes those views into one coherent check set. This is a strong
judgment default, especially for high-stakes checks, not a fixed agent graph or quorum.

**Context.** This is a lesson from experience, not theory. When agents were allowed to
*author* deterministic checks, they produced brittle, wrong-target, gameable ones — the
implementer inventing its own pass/fail criteria let it game itself. Judging a described
outcome removes that failure mode. **Important boundary:** the thing that failed was
*agent-authored* checks, not deterministic checks as a category. Running a check the
repo *already owns* (`pytest`, `import-linter`, `alembic upgrade`, exit codes) is
deterministic but is not that failure mode.

**Consequences.** The stock "deterministic forbidden" rule is correct for generic target
repos, where the only deterministic checks would be agent-invented. For a target that
already owns a trustworthy contract-test suite, the orchestrator may use both the judge
for qualitative outcomes and the repo's own deterministic suite as evidence, via a
dedicated workflow set rather than by loosening the stock template. Prefer a judge from
a different harness/model family than the primary implementer and check author where
practical. Prefer the `frontier` tier for difficult eval-policy/check design and
high-stakes judging when the session's capability roster offers it. A single judge pass
is evidence, never an engine-owned completion gate. Full reasoning:
`design/designs/success-semantics-and-evaluation.md` (Decision 2).

## D5. Full autonomy, with `unresolvable_error` as the only, last-resort human escape hatch

**Decision.** The design goal is to run **fully autonomously, with no human in the
loop**. Human involvement is a *last resort*, never a step in the normal flow. The one
sanctioned escape hatch is already built: a workflow that hits a genuinely terminal
blocker writes `control.json` with `stop_reason: "unresolvable_error"`, which stops the
session as terminal and leaves a recorded reason. We deliberately **do not** build a
preferred, resumable "pause and wait for a human to answer" gate.
For an identity-bound v2 or v3 session, D11's form applies: the producer must be the
exact current session/workflow/attempt, its role must be declared for terminal-blocker
reporting, and the record must list autonomous routes already tried. The frozen
assignment supplies the applicable schema version.

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
day one — a parent `planner` session that maintains program state and chooses a coherent
milestone or phase outcome, and a `dispatcher` that transports that outcome into a child
implementation session — rather than starting with a single flat loop and adding the
double loop later. The parent deliberately stays above leaf-task level. The child
`inner_outer_eval` orchestrator owns decomposition into work packages, tasks, reviews,
commits, and PRs.

**Context.** A single `inner_outer_eval` loop pointed at "build the whole thing" drowns
in context; the double loop gives each substantial phase or milestone a fresh child
planning context while the parent carries durable cross-cutting state. Sending an exact
leaf such as "execute WP-0.1" down from the parent defeats that boundary: it hoards the
real plan in the PM layer and leaves the child outer role unable to adapt. Milestone size
is a semantic judgment, not an engine limit; the planner may split or combine phases when
the live evidence warrants it.

**Consequences.** The parent/child machinery is on the critical path from day one, so it
must be hardened *first* — durable active-child crash recovery and a PM template that is
runnable from a clean init are prerequisites, not later polish. Both are implemented and
described in `designs/long-running-loop-reliability.md`. Session-stack recovery
reconstructs session/child *state* from files; it does not re-adopt a crashed worker's
agent subprocesses. A hard worker crash is handled by the D7 drain/reap cleanup path.
Child sessions remain depth-first and one-at-a-time (consistent with D2). The planner
drives the target's *own* authoritative plan; it does not invent a parallel backlog.
The stock PM workflow set contains `planner` and `dispatcher`; it does not duplicate the
child layer's scheduled eval roles. A target goal may point the planner to prepared
program-level evals and ask it to run them near the end, but that is semantic goal
context, not a generic protocol gate.

**Refined by D10 and D11.** The same depth-first session edge recurses beyond one child
level; three-depth dispatch, recovery, budget, and unwind tests guard that behavior.
Every layer makes its own scoped completion decision; child evidence, including optional
eval evidence, flows upward but cannot make that decision for an ancestor.

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
  recovery), with **reap** as the escape for hung-past-timeout or unsafe-to-finish work.
  `loopy stop --force` reuses this exact cleanup path with team-harness's explicit
  live-parent override after recording tree-wide stop intent; it does not implement a
  second process killer.
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
Force-stop has the same host and durable-run-record boundary: it reports an unreachable remote
worker or zero discovered runs honestly instead of claiming that untracked processes were
terminated.

## D8. Semantic constraints are visible detection with accountable repair or disposition, never hard prevention

**Decision.** The system handles semantic constraints by **detecting** relevant facts and
making them visible to the accountable orchestration role, which repairs the issue,
reroutes the work, or records why the finding does not apply. It never prevents the action
up front and never turns one generic observation into an unconditional engine veto. No
path-level write sandboxes, semantic scheduling vetoes ("this workflow may not run until X
is proven"), approval gates, arbitrary mid-run hard-fails, or mandatory eval gates.

This decision does not weaken structural protocol integrity. The engine still validates
schemas, identity, current-attempt ownership, hashes, path confinement, explicit budgets,
and state-machine topology. Those checks protect the durable machine from corrupt or stale
input; they do not decide whether the work product is semantically good enough.

**Context.** Stated as a general principle by the author (July 2026, during the design-loop
work in writeit-loops-and-standards): agents should have enough freedom to decide;
"fail-closed detection, not prevention" is the correct mental approach. It generalizes what
three existing decisions already do individually: D3 keeps mid-run "success" mechanical and
makes quality a separate, explicit orchestration decision; D4 bans *agent-authored*
pass/fail criteria while keeping repo/set-owned checks as useful evidence; D5 rejects a
preferred human gate in favor of evidence-in (`updates_from_user`) and a last-resort
terminal stop.
The reasoning: prevention encodes today's guess about what agents shouldn't do and hides its
own mistakes, while detection publishes every constraint as a visible, arguable check
failure with a defined relaxation route — a wrongly-scoped check gets repaired with a
counterexample and independent review instead of being silently obeyed forever. Concrete
shape: a workflow set may report that protected paths differ from their session-start
digest. The orchestrator sees the exact diff, normally repairs it, and can explain a
legitimate exception. The engine neither hides the action with a sandbox nor silently
promotes the report to terminal authority.

**Consequences.** New engine features and workflow sets must not introduce preventive
mechanisms: no coordinator-enforced path permissions, no eligibility gates keyed to semantic
state, no paused/waiting-for-human states (already banned by D5). Where discipline over
files is needed (a research workflow should not touch binding docs), express it as visible
diff evidence and clear prompt responsibility. A failing test, review, or eval is important
input, and ignoring it should demand an explicit rationale, but its existence alone does not
rewrite `HistoryEntry.success` or prohibit orchestrator-owned `control.json`. The accepted
cost is that a violating action can occur and must be detected and repaired after the fact;
that inefficiency buys inspectability and reversibility of the constraint itself.

## D9. Strong coordinators receive a frozen harness/tier roster; delegation and review remain prompt-guided

**Decision.** Every harness coordinator in a session tree — root PM, child
implementation, and any deeper layer — runs the same strong coordinator model
(`team_harness_model`, one value per repo). Every attempt also receives a frozen roster
of all enabled **harness families** and their configured `{model, effort}` bundles by
semantic strength tier. The canonical stock vocabulary is:

- `frontier`: the maximum-capability configured bundle for the hardest planning,
  architecture, adversarial review, and eval-policy/check design;
- `strong`: a high-capability bundle for complex reasoning, implementation, and review
  that does not require the maximum tier;
- `standard`: a balanced default for ordinary implementation, analysis, and review; and
- `economy`: a lower-cost/lower-latency bundle for bounded mechanical work, broad
  reconnaissance, and low-risk checks.

For orientation only, an Anthropic-family roster could map these tiers to
Fable, Opus, Sonnet, and Haiku respectively. Prompts use the semantic tier
names, never those provider-specific examples.

Harness family and strength tier are independent choices. A different family provides
diversity of tools and failure modes; a stronger tier provides more capability within a
family. The roster states unavailable family/tier combinations explicitly. Every
coordinator receives its absolute path and a rendered summary, then chooses family,
tier, concurrency, retries, and review shape dynamically via team-harness per-spawn
overrides.

For consequential planning, design, uncertain analysis, review, and especially eval
check creation, prompts should prefer independent analyses from different enabled
harness families in parallel when separable, followed by review by a family other than
the primary author. The accountable coordinator synthesizes disagreements. This is
guidance plus an audit trail, never a required number of agents, a fixed graph, a vendor
rule, or an engine gate (D8).

**Context.** Differentiating whole sessions ("strong parent, cheap child") was analyzed
and rejected: it complicates cost accounting and can downgrade the very coordinator
that must plan and judge the layer. Uniform strong coordinators avoid both problems,
while per-spawn tiers control the bulk worker cost. Earlier vendor-specific chains did
produce useful independent review, but hard-coded one graph and repeated volatile model
names in prompts. The frozen roster preserves that diversity benefit while allowing the
coordinator to adapt to availability, task coupling, cost, and live evidence.

**Consequences.** Stock prompts name semantic tiers and roster entries, never model IDs
or required vendors. Repository config may omit unavailable canonical mappings and may
add a clearly explained project-local tier; the frozen roster still renders every
canonical cell for every enabled family and marks missing mappings unavailable. Stock
prompts use `frontier`/`strong`/`standard`/`economy` and inspect that availability.
With `default_tier` set, the tier derives `team_harness_agent_models` and
`team_harness_agent_reasoning_efforts`, so concrete model IDs live in one place. The
engine validates roster shape and records requested/effective harness, model, and
effort; it does not judge whether a model deserves its label or reject a coordinator's
choice. Parallel delegates write separate findings or trace artifacts rather than
racing to edit one canonical plan or check set. If only one family is usable, the
coordinator proceeds autonomously and records the limitation when material. Do not add
per-depth model allowlists, spend vetoes, review quotas, or coordinator-model
differentiation per layer.

## D10. Durable loop layers recurse; harness subagents remain dynamic delegations

**Decision.** A durable loop layer is one loopy session with a scoped goal, state,
plan, decisions, accepted-work ledger, semantic handoff, attempts, optional eval
evidence, and optional child. One-layer, planner/dispatcher, and deeper systems compose
the same session node and parent→child protocol. Only the deepest session owns a live
loopy assignment. The workflow contract names one persistent orchestration role that
owns the layer's plan, handoff, and completion decision.

Inside that assignment, the team-harness coordinator remains free to choose a dynamic
team, roles, models, ordering, retries, and follow-ups. Spawned agents—including a
nested `type=harness` coordinator—are delegates in the current layer unless the
owning workflow explicitly publishes a child-session request.

Every attempt receives a frozen assignment with loop identity, role, responsibility,
and worker-local absolute state/output paths. It also receives the complete frozen
workflow roster, an attempt-frozen scheduler view with recent mechanical history and a
clearly conditional next-workflow forecast, and D9's frozen harness/model capability
roster. The forecast says what would run next if the current attempt returns normally
without terminal control, child dispatch, stop, or failure; it is context, not a
promise or eligibility gate. This lets an orchestrator avoid duplicating work that a
scheduled reviewer or evaluator is already due to perform.

Team-harness derives a smaller absolute assignment for each direct spawn. Durable
records use validated logical references so they survive a moved checkout. Child
request/input bytes are copied into the child's immutable `inputs/` area so later
parent edits cannot change accepted work. The workflow roster describes scheduled
Loopy roles, not a predicted harness-subagent graph; those delegates remain dynamic.

**Context.** “Inner loop,” “child,” and “subagent” blurred durable session depth,
workflow roles, and short-lived processes. Encoding a fixed agent graph would weaken
the coordinator, while relying on prompt authors to repeat ecosystem context left
delegates unsure of their layer and paths.

**Consequences.** D2 still permits parallel harness agents inside one assignment but
not parallel loopy workers. Ownership metadata is accountability, not an ACL or model
allowlist (D8/D9). Prompts should encourage independent parallel analysis and review by
other enabled harness families while leaving team shape to the coordinator. The same
edge is tested through three active depths. Provider-native nested actors are recorded
only when observable. See the
[binding design](designs/recursive-loop-layer-contract.md) for the full contract and
legacy boundary.

## D11. Every session's orchestrator owns completion; evals are optional evidence

**Decision.** Every durable session owns a scoped goal and names exactly one persistent
orchestration role that owns the layer plan, integrates evidence, publishes the semantic
handoff, and decides when that goal is complete. In the stock sets:

- `inner_outer_eval`: `outer` owns task selection and acceptance, the layer handoff,
  and terminal `goal_met`; and
- `pm_planner_dispatcher`: `planner` owns the program plan, child acceptance, root
  handoff, and terminal `goal_met`.

A child outcome is evidence for its parent, never proof that the parent's broader goal
is complete. Eval reviewer/runner roles, when configured or dynamically invoked, are
evidence producers. They may author or run checks on their schedule and publish
provenance-rich observations for a later orchestrator attempt. The orchestrator may
also run or delegate an eval directly, wait for a due scheduled eval role shown in its
scheduler view, rerun or supersede an observation, or decide that evaluation is
unnecessary. The stock PM workflow therefore needs only planner and dispatcher;
program-level prepared evals run near completion when the target goal asks the planner
to run them.

Successful control must identify the exact current session, workflow, and attempt; come
from the completion role frozen in the workflow contract; and contain a non-empty
reasoned completion disposition. It may cite the evidence considered, but evidence
references are not required to be non-empty. An eval receipt is optional and
need not come from the same attempt. When cited, the engine strictly validates its
session/goal identity, check definitions, producer/harness identity, judge settings,
declared runner role, raw and canonical report hashes, and evaluated git state. It
validates provenance, not the weight the orchestrator gives the verdict. An absent or
malformed advisory eval is
a visible diagnostic; it does not turn a mechanically completed harness invocation into
failure or starve the orchestration role.

D5 terminal-blocker control retains exact identity, allowed-role, attempted-route, and
evidence requirements. A spawned delegate reports upward to its harness coordinator; it
cannot publish durable control for another role, attempt, or layer.

**Context.** The first one-layer prompt made `outer` the persistent planner and used
scheduled evaluators as occasional independent observations. The first PM workflow had
only `planner` and `dispatcher`, with planner owning the program decision. The v2
provenance work fixed real stale/sibling/goal ambiguity, but overcorrected by moving
completion authority to `eval_runner` and requiring a same-attempt passing eval. That
made one advisory mechanism mandatory, duplicated evaluation at PM and child layers,
and could prevent a smart orchestrator from finishing despite stronger direct evidence.
The required boundary is exact identity and truthful provenance, not semantic deference
to a particular scheduled role.

**Consequences.** Remove terminal control from the eval sub-contract and represent the
orchestration/completion role at top level. Remove the universal same-attempt passing
receipt and `goal_check.json` prerequisite. Invalid advisory eval output becomes a
durable diagnostic rather than `goal_check_broken` or a generic workflow failure.
`inner_outer_eval` remains fully standalone: root and nested sessions use identical
planning, optional-evidence, handoff, and completion semantics. Fresh stock or explicitly
amended-v3 sessions use the new contract; custom sets retain the version they declare,
a contract file with no version remains pinned to the historical v2 default, and
already-live sessions retain their frozen historical contract rather than changing
authority midway. See the
[binding design](designs/orchestrator-owned-completion-and-cross-harness-review.md).

## D12. Correctness state/evidence and exhaustive execution traces have separate retention contracts

**Decision.** Compact facts required to schedule, recover, or justify acceptance stay
with the session: topology, goals, assignments, layer plans and accepted-work ledgers,
progress, decisions, semantic handoffs, workflow/scheduler/harness rosters, normalized
results, orchestration completion rationale, and optional eval plus
git/delivery/recovery receipts. Detailed observable
execution lives under separately gitignored `.loopy_loop/traces/`: prompts, visible
turns, tool/spawn I/O, process/provider identity, streams, raw eval output, verbose git
evidence, timing, and usage. Inputs are persisted before their provider calls.

Independent analyses and reviewer transcripts stay in the trace plane. The accountable
coordinator promotes conclusions that future attempts need into compact plan, decision,
eval-definition, accepted-work, or handoff artifacts; continuity never depends on
re-reading raw conversations. Raw eval bytes are validated when a canonical receipt is
accepted and sealed; later citations trust that compact accepted receipt and subject
identity rather than requiring retained trace bytes.

Each attempt has one caller-owned, completeness-aware trace manifest. The coordinator
creates it during dispatch; the worker and team-harness populate that same canonical
tree. Matching completion or crash abandonment uses a write-ahead finalization record,
then a hashed manifest and compact session-side seal receipt. Startup retries only
after durable history proves the transition committed; an unavailable HTTP response
is recorded as unavailable rather than invented. Trace I/O failure and advisory-eval
failure are observable but never become semantic acceptance gates (D3/D8/D11).

**Context.** Before this decision, execution records were split inconsistently:
session-local worker artifacts lived under harness outputs, while team-harness 0.4.0
wrote its complete coordinator `run.json` to a global directory. Loopy's
successful-run usage reader looked in the session location even though recovery used
the global location, so the contract was not self-contained: ordinary successful
usage was unknown and `max_cost_usd` could not fire against that integration. Prompts
and raw logs also shared retention boundaries with semantic state, making later cloud
analysis and independent retention unclear.

**Consequences.** D1 is refined, not replaced: continuity remains inspectable files
and git. “All I/O” means observable/model-visible logical I/O; hidden reasoning and
provider-internal bytes remain unavailable, and unavailable channels are stated
honestly. Raw traces are sensitive local data; correctness-critical facts do not rely
on their retention. See the [binding design](designs/recursive-loop-layer-contract.md)
and [session layout](../docs/session-layout.md).
