# Executive summary

`loopy-loop` is a promising durable control shell for long-running agent work, but it is not yet a trustworthy autonomous project manager for a system as large as UGM. Its strongest idea is the right one: continuity belongs in repository files, git, explicit workflow prompts, and inspectable artifacts rather than in one ever-growing chat. Its weakest seam is also clear in the code: mechanical completion is too easily promoted to semantic success. `team-harness` may return normally after failed or prematurely abandoned workers; `loopy_loop.harness_runner._normalize_harness_result()` then marks the iteration successful; and the stock eval workflow asks an agent to author and judge high-level LLM checks while explicitly forbidding deterministic checks. Before adding parallel loopy workers or a dashboard, the maintainer should harden the state machine, make active-child recovery durable, introduce typed outcomes and an append-only event/cost model, and make externally owned contract tests the authority for completion. UGM is an excellent dogfood target only as a sequence of narrow, human-gated work-package pilots—starting with one bounded Phase 0 package—not as a 120/160-turn autonomous run over the roadmap. The overall bet on long-horizon agent coding is sound as supervised, evidence-gated automation; the claim of unattended end-to-end autonomy is not yet supported.

# Q1. Future improvements to loopy-loop

## Current technical assessment

The codebase is small enough to reason about: roughly 2,769 lines under `src/loopy_loop`, with 116 tests. I ran the suite: all 116 passed. Ruff and `uv run pyright` also pass. The fundamentals are better than the Alpha label might suggest:

- `StateStore.mutate()` holds a file lock and commits `state.json` with `os.replace()`, giving coordinator transitions a useful atomic core.
- Root configuration is captured in `LoopState.config_snapshot`, so a worker does not silently take a new model setting from the root YAML halfway through a session.
- The `pending_finished_request.json` plus `result.json` recovery path closes an important crash window between harness completion and `/finished` acknowledgement.
- The scheduler has explicit and well-tested cadence semantics, including failed-workflow retry without incorrectly unlocking `must_follow` dependants.
- Signals use strict Pydantic schemas and schema versions.

Those strengths should be preserved. The next work should focus on correctness and proof, not breadth.

## Ranked priorities

Effort estimates assume one maintainer already familiar with the code, include tests and docs, and exclude elaborate UI polish.

| Rank | Improvement | Rough effort | Why now |
|---|---|---:|---|
| 1 | Make dispatch and session-stack recovery a real state machine | 2–3 weeks | There are crash/restart paths that can lose the active child or duplicate live work. This is correctness, not ergonomics. |
| 2 | Define typed execution, acceptance, and failure outcomes | 1–2 weeks | A normal harness return is currently equivalent to success even if the work is bad or workers failed. Failure strings are too lossy for policy. |
| 3 | Implement the event/usage/cost ledger, then a live status view | 2–4 weeks | `events.jsonl` is empty by design today. Without a canonical event stream, operations, debugging, budgets, and a TUI would all invent separate truth. |
| 4 | Replace the stock eval policy with deterministic-first evidence gates | 4–7 days | The current template's “only harness_judge; deterministic forbidden” rule is backwards for software delivery and unsafe as a stop gate. |
| 5 | Finish the operator experience: `doctor`, `run`, session-aware `status`, pause/resume, and force-stop | 1–2 weeks | The current CLI hides active children and cannot interrupt a running harness. It is too easy to operate the wrong session. |
| 6 | Version and validate configuration/runtime compatibility | 1–2 weeks | Model IDs and CLI contracts churn across three repos; state/config schemas have no explicit migration layer. |
| 7 | Refactor `CoordinatorService` around one transition function | 3–5 days | It lowers the defect rate for ranks 1–3. It is valuable, but refactoring alone is not the product priority. |
| 8 | Strengthen child-session contracts and budgets | 1–2 weeks | Child requests lack stable request IDs, per-child turn/cost/model budgets, explicit outcomes, and durable active-child ownership. |
| 9 | Add fault-injection and real dependency integration tests | 1–2 weeks initially, ongoing | The unit suite is strong on known branches but misses restart, race, corruption, disk, and live-CLI boundaries. |
| 10 | Reconsider multi-worker loopy-loop execution only after isolation exists | 4–8 weeks | Parallel work on one checkout would trade elapsed time for nondeterministic repository corruption and merge conflict. It is not the next bottleneck. |

### 1. Make dispatch and session-stack recovery a real state machine

The biggest immediate problem is that `/register` is both “give me work” and “proof that the previous worker is dead.” In `CoordinatorService.register_worker()`, any existing `current_task` is recovered from local artifacts or immediately recorded as `error="abandoned"`. A second accidentally started worker, a worker reconnecting during a network partition, or a worker still running while the coordinator is restarted can therefore cause duplicate work. The first worker may still be editing the same checkout when the second one receives a replacement task. Removing leases simplified v0.2.0, but it also removed the coordinator's basis for deciding that a task is actually orphaned.

Do not restore the old distributed worker system wholesale. For the intended single-worker product, make the invariant explicit:

- A normal `/register` while `current_task` exists returns `busy`/the existing assignment without mutating it, or returns HTTP 409 with an actionable message.
- Recovery is a separate explicit operation: `resume-task`, `force-abandon`, or coordinator startup recovery after a configurable grace period and process/heartbeat evidence.
- Add an `attempt_id` UUID. The current key `(session_id, workflow_id, iteration)` cannot distinguish a legitimate retry from the original execution.
- Persist `last_worker_contact_at`, execution PID/host when local, and a recovery decision with reason. Worker identity need not return as a scheduling abstraction; a per-attempt owner token is enough to prevent stale writers.

Child recovery is more urgent. While a child runs, the active state is held only by `CoordinatorService.state_store` pointing at the child's `state.json`. The parent has `children.json`, but no durable `active_child_session_id`. After a coordinator process crash, a new `StateStore(repo_root=...)` chooses `latest_top_level_state_path()`, so `--resume` reopens the parent. The consumed child request is gone, the child can remain marked `running`, and the parent can dispatch new work. There is no test for restart during an active child.

Persist the session stack in the root state (or a small root `runtime.json`) and make startup reconstruct and validate it. A transition should atomically mean “parent suspended on child X; child X is active.” On restart, terminal children should be finalized and the parent resumed; non-terminal children should remain active. `children.json` should be an index/projection, not the only bridge.

Other recovery details to cover in the same work:

- Write `children.json`, `result.json`, `pending_finished_request.json`, and control/eval artifacts atomically. Currently only `state.json` gets the temp-file/replace treatment.
- Give invalid child request files a terminal rejection record instead of silently leaving them in the directory.
- Enforce at most one outstanding child request rather than relying on prompts.
- Define what happens when the repository has changes from an abandoned attempt. Retrying is not automatically safe merely because the harness result file is absent.
- Add `state_schema_version` and migration code. Relying on Pydantic dropping old fields, as the v0.2.0 changelog describes, is not a durable resume strategy.

### 2. Define typed execution, acceptance, and failure outcomes

`run_harness_iteration()` distinguishes exceptions, but `_normalize_harness_result()` creates `IterationResult(success=True, ...)` for every normal `TeamHarnessResult`. That is only “the coordinator model stopped without raising.” It does not mean the requested change was completed, tests passed, or every worker succeeded. `TeamHarnessResult.agents` is discarded even though it contains statuses and exit codes.

Replace the Boolean with at least three layers:

1. `execution_outcome`: `completed | coordinator_error | worker_error | timed_out | cancelled | protocol_error`.
2. `artifact_outcome`: whether required files/signals/reports exist and validate.
3. `acceptance_outcome`: `accepted | rejected | inconclusive | not_evaluated`, set only by a configured verifier or deterministic contract.

Scheduling should key off explicit policy. An implementation workflow may be mechanically complete but still require review; a failed worker may be tolerable if another worker delivered and verification passed; an empty coordinator final response should be inconclusive. Preserve the full agent summary and structured `TeamHarnessError.detail` in `IterationResult` and history.

The failure taxonomy should include, at minimum:

- coordinator auth/rate-limit/provider/transient exhaustion;
- worker binary missing, worker auth/API failure, non-zero exit, timeout, killed-on-finalization;
- malformed coordinator response/tool protocol;
- invalid/missing control, goal-check, child-request, or eval report;
- repository conflict/dirty-state policy violation;
- verification failed vs. verification unavailable;
- operator cancellation, stale attempt, forced abandonment;
- infrastructure unavailable and human-decision required.

Each class needs retryability, backoff, maximum attempts, and whether it consumes the work package's turn budget. Today a generic harness exception becomes a failed iteration and the scheduler can retry until `max_turns`, spending money without distinguishing a transient 429 from a deterministic test failure.

### 3. Implement events, usage, and cost before building a dashboard

`sessions.create_session_dir()` creates `events.jsonl`, and the docs call it reserved, but no code appends to it. Fill that contract before choosing a TUI framework. Emit one versioned event per significant transition:

- session created/resumed/paused/terminated;
- task selected/started/heartbeat/completed/accepted/rejected/abandoned;
- child requested/rejected/started/completed/parent-resumed;
- control and goal-check signal read/invalid/applied;
- retry/backoff and structured failure;
- operator update/stop/force-stop;
- git baseline/head/dirty summary;
- coordinator and worker model identities, provider/CLI versions, token usage, duration, and cost estimate.

Events need an `event_id`, `schema_version`, UTC timestamp, root/child session IDs, task and attempt IDs, causation ID, and a small typed payload. Append under the same session lock; tolerate a truncated final line. Derive status views from state plus events, not from ad-hoc directory scanning.

`team-harness` already records coordinator usage per turn in its `run.json`, but does not expose aggregate usage in `TeamHarnessResult`; worker CLI usage is provider-specific and often only present in stream output. Add a usage adapter with `known/unknown` fields rather than pretending all costs are measurable. Store price-table version and currency beside estimates because model prices change. Budgets should be enforceable at session, child, workflow, model, and wall-clock levels, checked before dispatch and after every harness result. Unknown usage should be visible and policy-configurable, never silently zero.

Once the event model exists, a useful first UI is modest:

- `loopy status --watch` with root/active-child tree, current attempt age, last event, spend, and next eligible workflow;
- `loopy events --follow --json` for automation;
- later, a Rich/Textual TUI reading exactly the same event/state APIs.

A web dashboard is not justified yet. A local TUI covers the actual solo-maintainer deployment without adding another service and authentication surface.

### 4. Change the eval template's philosophy

The `inner_outer_eval/eval_reviewer` prompt says “Only create harness_judge checks” and “Deterministic checks are forbidden.” I disagree with this strongly. In software engineering, objective checks should dominate whenever possible: tests, static analysis, import boundaries, migrations, command exit codes, schema invariants, and golden-set metrics. LLM judges are useful for residual qualitative properties, not as substitutes for facts a program can establish.

The stock policy should be:

1. Run repository-owned deterministic contracts first.
2. Fail closed on missing, changed, or errored required checks.
3. Use a judge only for explicitly qualitative criteria.
4. Use a different model family from the implementing agent where practical.
5. Require repeated/consensus judgments for a terminal stop, or retain human approval for high-impact goals.
6. Prevent the implementation branch from weakening its own required checks without a separately reviewed change.

For eval-banana, the eval runner should parse `report.json` itself and derive `goal_check.json`; the agent should not be trusted to paraphrase console output into the stop signal. A report path and hash should appear in the goal-check artifact.

### 5. Finish the operator experience

The existing `init`, `status`, and `stop` commands are a good skeleton but misleading around child sessions:

- `status` uses the latest top-level state, so while the service is executing a child it can show the suspended parent and no current task.
- `stop` sets `stop_requested` on that top-level parent. It does not interrupt or even directly target the active child; the request only takes effect after the child eventually terminates and the parent resumes.
- A running `TeamHarness.run()` has no cancellation channel from loopy-loop. Stop is observed between iterations only.

Add:

- `loopy doctor`: Python/package versions, coordinator model capability, configured worker binaries/auth, eval-banana availability, git status, writable session directory, port availability, and config compatibility.
- `loopy run`: supervise coordinator plus one worker locally, handle signals, print the session ID, and keep the two-process mode available for remote execution.
- `loopy sessions list/show/tree`, with explicit `--session` everywhere.
- `loopy status --json/--watch`, including active child and stale-attempt diagnosis.
- `loopy pause` (finish current task, dispatch nothing), `resume`, `stop` (cooperative), and `stop --force` (cancel harness/process with a recorded outcome).
- `loopy validate` for workflow graphs, template dependencies, signal schemas, and required external tools without starting a session.

The packaged `pm_planner_dispatcher` template currently tells the dispatcher to request workflow set `inner_outer_eval`, but `loopy init --template pm_planner_dispatcher` copies only the planner and dispatcher workflows. The child set is absent unless the user separately initializes/copies it. The current template preflight test checks only the parent. This should be fixed by bundling the child set or by making workflow-set dependencies declarative and installable; it is a concrete onboarding failure.

### 6. Stabilize configuration without freezing model IDs

Model names are repeated in `config.py`, `cli.py`, packaged YAML, README/docs, `.team-harness/config.toml`, `.eval-banana/config.toml`, and the two sibling projects. That guarantees drift. The answer is not a forever-stable default model name—there will not be one.

Use named execution profiles, for example `balanced`, `high_assurance`, and `cheap`, resolved in one project-local file to exact provider/model/effort values. Scaffold a profile with comments and require `doctor` to validate it. Package defaults should prefer the provider/CLI's supported default or a centrally declared compatibility recipe, not duplicate a model string across templates and prose. Snapshot the resolved exact values into the session.

Pin the critical dependency as `team-harness>=0.2.10,<0.3` while both projects are pre-1.0 and breaking changes are expected. Add a tested compatibility matrix and an adapter layer in loopy-loop, rather than constructor-signature introspection as the long-term compatibility policy. Eval-banana is not a hard dependency, so templates that require it must either declare/install an extra (`loopy-loop[eval]`) or fail preflight with a clear instruction.

Also snapshot workflow definitions and prompt hashes. Root config is snapshotted today, but the worker reloads `prompt.txt` and per-workflow `config.yaml` from disk, and the coordinator reloads workflow definitions. A resume after prompt/scheduling edits is therefore not a replay of the original session. Either deliberately version/live-reload these files with change events, or store immutable copies in the session and make `--resume-with-current-workflows` explicit.

### 7. Refactor coordinator duplication as part of the state work

The repeated blocks in `register_worker()` and both stale/matched branches of `finish_assignment()` all do:

1. apply stop conditions;
2. dispatch a requested child;
3. choose a workflow;
4. set `current_task`;
5. build a run response.

Extract a single `_advance(state, cause, now) -> TaskResponse` after defining the state-transition table. Keep record/recovery parsing separate from advancement. This will remove roughly three copies of terminal/no-eligible/new-task behavior and make event emission occur once. I would not do a cosmetic extraction first: encode the new invariants and table-driven transition tests at the same time, or the refactor merely centralizes current ambiguities.

### 8. Improve child sessions before allowing arbitrary nesting

Depth-first, one-child-at-a-time is a sound v1 choice. It matches a shared checkout and makes parent review comprehensible. Do not add breadth-first child parallelism yet.

What is missing is contract richness:

- stable `request_id` and parent work-item ID;
- required acceptance artifact names;
- child `max_turns`, wall-clock, token/cost budget, execution profile, and retry policy;
- `requested | accepted | rejected | running | paused | terminal` lifecycle;
- explicit outcome and evidence bundle returned to the parent;
- durable active-child pointer and nested session stack;
- maximum depth and total descendant budget.

The current child inherits root configuration through `_preflight_for()`; the request schema contains only `workflow_set`, `goal`, and `schema_version`. A PM parent cannot set a small child budget or a different verifier/model profile. That is a practical blocker for UGM.

### 9. Testing gaps

The 116 tests cover the intended scheduler and HTTP branches well, but confidence is concentrated in mocked, single-process happy paths. Add tests for:

- coordinator restart while a child is running and after the child becomes terminal but before parent resumption;
- two `/register` calls while a task is live, late `/finished` from an old attempt, and network partition timing;
- stop/status while a child is active and force-cancellation during a harness run;
- crash/disk-full/partial JSON at every artifact handoff;
- workflow/config edits between start and resume;
- invalid and duplicate child requests;
- state migration from every released schema;
- real subprocess integration against a fake agent CLI, plus a scheduled smoke matrix for actual supported CLIs;
- team-harness returning failed/killed agents with a normal final response;
- property-based scheduler/state-machine invariants;
- packaging tests proving every scaffolded template can execute its first child/eval dependency from a clean temp repo.

### 10. Do not prioritize parallel loopy workers

This is where I disagree with the obvious “scale means more workers” answer. `team-harness` already provides parallelism inside an iteration. Loopy-loop coordinates stateful repository mutations in a single checkout. Two loopy assignments editing overlapping files, migrations, design status, or git branches would require worktree isolation, declared dependency/resource locks, deterministic merge/review, and conflict recovery. Reintroducing leases is the smallest part of that job.

Parallel loopy workers become worthwhile only when tasks are explicitly read-only or allocated isolated worktrees/branches with a merge coordinator. UGM's early work packages are highly serial: schema, ports, worker substrate, and eval harness establish contracts consumed by everything else. The near-term throughput constraint will be human decisions and evidence quality, not worker occupancy. Keep one loopy assignment at a time and make team-harness's internal delegation observable and budgeted.

# Q2. Is loopy-loop up for implementing UGM?

## Verdict

Not end to end, unattended. Yes for a deliberately narrow pilot with human-owned gates and deterministic acceptance.

UGM is not merely a large coding task. It is a sequence of architecture, data-quality, infrastructure, model-selection, human-labeling, and operations decisions. The roadmap has nine numbered phases (0 through 8), dozens of work packages, a 2,375-line Postgres schema design, about 6,938 lines of design documents, 64 recorded decisions, and open decisions/spikes that deliberately block implementation. The current code is six executable statements plus two smoke tests. I ran its current checks: Ruff, formatting, pyright, and both tests pass, but there is effectively no product implementation.

Loopy-loop can provide repetition and durable artifacts. It cannot turn unresolved product authority into an autonomous decision without violating UGM's own design.

## The stock `pm_planner_dispatcher` is directionally right but insufficient

The parent-planner/child-implementation shape is better than running `inner_outer_eval` over “build UGM.” One work package per child is the maximum sensible scope. The parent can review terminal child evidence and keep the larger roadmap out of the child's immediate context.

I would not use the stock template unchanged:

- Its child workflow dependency is not installed by the PM template, as noted above.
- The parent and child share one root config and therefore one `max_turns` and execution profile unless the runtime is extended.
- Work-item identity exists only in prompt-maintained Markdown, not in `ChildSessionRequest` or `children.json`.
- The planner is allowed to create/refine backlog items from a prose goal. UGM already has an authoritative roadmap; the agent must not invent an alternative backlog.
- The generic inner/outer prompts are extremely long and themselves instruct team-harness workers to create further agent teams, research, plan, review, execute, open PRs, and merge. Nested delegation can multiply cost and make responsibility unclear.
- The eval reviewer forbids deterministic tests, directly conflicting with UGM's contract-test and measured-golden-set philosophy.
- Parent stop/pause and active-child restart semantics are not strong enough for multi-day WPs.

Build a custom `ugm_phase_driver` parent workflow set derived from the PM template, with these workflows:

1. `gate_reconciler`: compare the phase entry gates and WP dependency/status projection against authoritative decisions and repository evidence; pause on unresolved human gates.
2. `wp_selector`: select exactly one already-defined eligible WP. It may narrow execution into PR-sized slices, but cannot alter acceptance criteria or mark gates resolved.
3. `dispatcher`: create a typed child request containing phase/WP ID, design reads, exact acceptance contracts, immutable check set/hash, and budgets.
4. `evidence_reviewer`: import child artifacts and independently run/inspect acceptance. Only this workflow updates WP status or accepted-completion records.

The child should be a smaller `ugm_wp` workflow set, not the generic 160-turn loop:

- `plan`: read only the WP's `Reads` list plus `concepts.md §0`, produce a bounded plan and surface design conflicts.
- `implement`: implement one PR-sized slice.
- `contract_eval`: run repository-owned commands and parse machine reports; `emits_goal_check: true`.
- `review`: inspect the diff and evidence with a different model family if an LLM review is useful.

For small WPs, run the contract eval after every implementation success, not after ten successful `inner` iterations as the stock cadence does.

## Where the model breaks on UGM

### Human decisions are not failures

UGM has legitimate decision gates: model seats, PageIndex deployment, budget/corpus assumptions, HA/observability, belief scope, hard-delete design, rename/CLA. Loopy-loop only understands terminal goal success, terminal unresolvable error, stop request, max turns, or scheduling failure. It needs a non-terminal `paused`/`waiting_for_human` state.

Add a versioned `gate_request.json` contract:

```json
{
  "schema_version": 1,
  "gate_id": "llm-stage-models",
  "blocks": ["WP-1.3"],
  "question": "Which extraction model/profile is approved for the Phase 1 pilot?",
  "options": ["..."],
  "recommendation": "...",
  "evidence_paths": ["..."],
  "requested_at": "..."
}
```

The coordinator should stop dispatching, keep the session resumable, show the gate in `status`, and accept an operator decision with author/timestamp. Do not encode this as `unresolvable_error`; that makes a normal governance checkpoint look like a failed terminal session.

### The roadmap is already stale enough to fool an autonomous planner

The repository provides direct evidence of the danger:

- `plan/plans/phase-0-foundations.md` still marks `WP-0.1` as `blocked(stack-conventions)`, while commits `d6abccf`, `cba62f4`, and `ec5ce3a` merged the package/tooling/CI scaffold into `main`.
- `roadmap.md` and `phase-1-walking-skeleton.md` still list embedding choice #3 as an entry blocker, while `questions.md` marks it resolved by D63 and `decisions.md` contains D63.
- `questions.md` says observability stack #10 should be decided “before the first worker,” while the roadmap gate table maps it to Phase 7.

An agent following the phase file literally will stop on resolved work; an agent following git literally may declare a WP done without updating the authoritative status. Before automation, add a machine-readable status/gate projection and CI consistency checks. Markdown can remain canonical, but a parser should validate that resolved question references, gate register status, WP statuses, merged evidence links, and phase entry gates agree. The driver should fail closed on inconsistency and request reconciliation, not guess which document wins.

### Infrastructure and money require policy boundaries

Phase 0's reference path includes Postgres, GCP Cloud Tasks/Run, GCS/gcsfuse, and later Hetzner. A headless agent can write Terraform, compose files, adapters, and mocked/service-container tests. It should not silently create billable cloud resources, choose regions, modify production IAM, buy a Hetzner server, or decide a secrets policy. The stock prompts' broad permission language is too permissive for this project.

For the pilot:

- use the self-host profile and ephemeral Postgres/MinIO service containers;
- make cloud adapters contract-testable with fakes/emulators;
- require an explicit `external_action_request` for billable or destructive operations;
- pass credentials only through UGM's required `pydantic-settings`/`SecretStr` surfaces;
- prohibit agent-authored secrets and record every external mutation.

### Golden sets need humans and protected data

D22 explicitly requires human-adjudicated measurement labels and guards against circularity. Agents can propose pairs, create labeling tools, and run metrics. They cannot manufacture the gold labels and then claim measured quality. Real corpus data may also be unavailable or privacy-sensitive.

The UGM repo should distinguish:

- frozen, human-owned golden labels;
- synthetic test fixtures agents may create;
- agent-proposed labels awaiting adjudication;
- measured thresholds and the exact dataset/model/config version that produced them.

Do not let an implementation child edit a required golden set, acceptance threshold, or eval definition in the same acceptance transaction. Changes to those assets need a separate review path.

### Design conflict must pause implementation

The roadmap correctly says that a WP requiring deviation from design is a design change and must stop. The child prompt must carry this as a hard output contract, not just prose. A `design_conflict.json` should name the WP, cited sections, conflict, options, and recommended amendment. The parent pauses or dispatches a design-only child; it never allows implementation to quietly choose an architecture.

## What to add before a serious UGM run

### In loopy-loop

- Durable active-child/session-stack recovery and task attempt ownership.
- `paused/waiting_for_human` plus typed gate and external-action requests.
- Per-child turn, time, cost, model, and retry budgets.
- Structured outcomes and acceptance evidence, not Boolean harness success.
- Event/cost ledger and session-aware status/stop.
- Workflow-set dependencies so the PM template is executable from a clean init.
- Frozen workflow/prompt/check hashes per session.
- A direct deterministic verifier hook that does not require another LLM to translate reports.

### In UGM

- A reconciled gate register/WP status with evidence links to the already merged scaffold.
- A machine-validated projection of gates, WP IDs, dependencies, statuses, design reads, acceptance commands, and protected eval assets.
- `.loopy_loop/workflow_sets/ugm_phase_driver` and `ugm_wp` prompts that obey the roadmap rather than inventing a parallel plan.
- One goal file per phase or, preferably, per WP; never “implement UGM.”
- Exact `make`/`uv` acceptance commands and report formats for each WP.
- Local disposable infrastructure recipes before any reference-cloud execution.
- Human-decision and external-action inboxes with explicit ownership.
- A branch/worktree policy. One child at a time should own the checkout; evidence review should use the resulting commit/PR, not race it.

## Recommended pilot

Start with one Phase 0 work package, not all of Phase 0 and not the Phase 1 walking skeleton. First reconcile and close `WP-0.1` against the work already merged. Then use **WP-0.4 (ports plus import-linter contracts)** as the coding pilot: it is bounded, does not need billable infrastructure or golden labels, has a deterministic acceptance criterion (“an illegal import fails CI”), and exercises architecture, tests, review, PR delivery, and durable evidence.

Do not start the Phase 1 walking skeleton until Phase 0's migrations, local queue/profile, and eval harness exist and the extractor model seat (#4) is explicitly chosen. Phase 1 is a good second pilot because it is vertically integrated, but it already requires Postgres, Lance, embedding configuration, extraction calls, and scenario contracts; using it first would make failures hard to attribute.

An immediate single-WP root configuration could look like this using the current schema and the model name currently used by the checked-in templates (the exact model should be validated by `doctor`, not copied blindly):

```yaml
goal_file: ".loopy_loop/goals/wp-0.4.txt"
workflow_set: "ugm_wp"
max_turns: 12
goal_check_consecutive_failures_cap: 2

team_harness_provider: "codex"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
  - "claude"
team_harness_agent_models:
  codex: "gpt-5.5"
team_harness_agent_reasoning_efforts:
  codex: "high"

team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
team_harness_system_prompt_extension: |
  This session implements only WP-0.4.
  plan/plans/phase-0-foundations.md and the WP Reads list are binding.
  A design conflict pauses the session; do not improvise architecture.
  Required checks and import contracts may not be weakened to obtain a pass.
```

The corresponding goal should name only WP-0.4, quote its deliverable and acceptance criteria, list exact allowed design reads, prohibit cloud mutations, and require `uv run ruff`, `uv run pyright`, `uv run pytest`, import-linter, diff review, and PR evidence. Configure `contract_eval` after every implementation success. Twelve turns is still generous; a healthy pilot should finish in fewer.

If testing the parent/child design is itself the goal, use a parent capped around 8–12 PM turns and a child capped around 8–12 turns—but current loopy-loop cannot set those independently. Add per-child budgets first rather than giving a child the PM template's current `max_turns: 120`.

# Q3. Documentation and an official loopy-loop launch

## Launch position

The README is strong for an Alpha project, the two contract documents are useful, the release pipeline uses PyPI trusted publishing, and the changelog is candid about v0.2.0's breaking API. That is enough for an early adopter release, not an “official launch.” The product still has a correctness gap in child resume, an incomplete PM scaffold, no live operational view, no cost story, and stale Agent Skill instructions.

The Agent Skill is a pre-launch blocker. `skills/loopy-loop/SKILL.md` says state lives at `.loopy_loop/state.json`, describes “one or more blocking workers” polling HTTP, shows inline `goal`, and documents the removed `.loopy_loop/workflows/<id>/` layout. The current runtime requires a goal file, workflow sets, session-local state, and a single-worker ping-pong protocol. A skill that teaches agents an obsolete API is more damaging than missing documentation because it confidently generates broken setups.

The README also says “one or more workers” even though `coordinator_app.py` is explicitly safe only under its single-worker assumption and a second register can abandon live work. Correct that claim before public promotion.

## Missing documentation

### A real end-to-end tutorial

Create a copy/paste tutorial against a tiny maintained demo repository. It should show:

- prerequisites and exact tested versions;
- install and `loopy doctor` output;
- init, the generated files, and the edits the user makes;
- coordinator/worker or `loopy run` terminal output;
- expected `status` output before, during, and after an iteration;
- the resulting session tree and how to follow harness/eval evidence;
- stopping, crashing, and resuming;
- expected token/time/cost range from measured runs;
- cleanup and common failure output.

The current README explains components but does not let a newcomer compare their terminal to a known-good transcript.

### Concept and architecture guide

Explain the three nested control layers clearly:

```text
loopy-loop session/workflow scheduler
    -> one team-harness coordinator run per workflow iteration
        -> external coding-agent CLI subprocesses
    -> optional eval-banana checks as evidence
```

State which layer owns durability, retries, concurrency, cancellation, model selection, and success. Many users will otherwise assume “worker” means a team-harness worker or that loopy-loop resumes model conversations. Include the coordinator state transition diagram, crash windows, root/child session stack, and trust/security model.

### Workflow authoring guide

Document scheduling semantics with worked traces, not just field definitions. Include:

- priority/tie behavior;
- `run_every`, `must_follow`, `run_on_start`, and `run_after_successes` interactions;
- failure/retry behavior;
- `emits_goal_check` vs. actual stop control;
- workflow-set dependencies and child request lifecycle;
- prompt ownership, artifact contracts, and idempotency rules;
- patterns for implement/review/eval, human gates, and read-only research workflows;
- anti-patterns: overlapping writers, self-authored acceptance, huge goals, and prompt-only locks.

Ship a config schema/reference generated from Pydantic so prose does not drift.

### Troubleshooting and recovery

Cover missing binaries/auth, unsupported model IDs, API retries, invalid signals, stale tasks, dirty repos, corrupt state/artifacts, coordinator vs. worker crashes, active children, exhausted max turns, and how to decide between resume, retry, force-abandon, and a fresh session. Every error should point to this guide and the relevant artifact.

### Cost, security, and operating expectations

Publish measured ranges for each template: number of harness coordinator calls, likely spawned workers, eval frequency, typical wall time, and known unknowns in worker-token accounting. Explain that default worker templates use approval/sandbox bypass flags and therefore execute with the user's filesystem and credentials. Document safe use in disposable containers/worktrees, secret handling, network access, PR/merge permissions, and why untrusted repositories/prompts are unsafe.

### Versioning and compatibility

Declare which surfaces are public:

- CLI commands/exit codes;
- root/workflow YAML schema;
- HTTP contract;
- session layout and event/signal schemas;
- Python SDK, if any;
- template semantics.

For Alpha, say they may break on minor releases with migration notes. For Beta, commit to migrations for session state and deprecation for config/CLI. For 1.0, use SemVer for public surfaces, support reading at least the previous major session schema or provide an offline migrator, and publish a team-harness/eval-banana/CLI compatibility matrix.

## Positioning against alternatives

Do not position loopy-loop as a better coding agent, a general multi-agent SDK, or a distributed workflow engine.

- Ralph-style loops already offer fresh-context iterations with git and small progress/PRD files. Their advantage is radical simplicity. The public [snarktank Ralph implementation](https://github.com/snarktank/ralph) explicitly uses git history, `progress.txt`, and `prd.json`; loopy-loop must justify its extra machinery with typed workflows, inspectable per-iteration artifacts, crash recovery, child sessions, and evidence gates.
- OpenHands provides a broad software-agent SDK, state lifecycle, remote workspaces, server isolation, and scaling. Its [official SDK overview](https://docs.openhands.dev/sdk/index) positions it as a production-ready agent engine for local or cloud use. Loopy-loop should present itself as a durable repository-level workflow controller that can call such engines/CLIs, not compete on agent runtime breadth.
- Native Codex/Claude/Gemini automation and CI coding agents own their ecosystems and UX. Loopy-loop's differentiation is model-agnostic orchestration across repeated fresh runs with repo-native state and user-authored workflow/eval contracts.

A concise position would be: **“A durable, inspectable control loop for repository-scale agent work. Bring your coding agents; loopy-loop schedules repeatable workflows, preserves evidence, and resumes from files and git.”** Avoid “autonomous software engineer” claims until measured long-run evidence exists.

## Concrete pre-launch checklist

### Release blockers

- [ ] Fix active-child restart/recovery and second-register semantics.
- [ ] Make stop/status session-stack aware and document cancellation limits.
- [ ] Introduce typed outcomes; do not equate normal team-harness return with accepted work.
- [ ] Populate versioned `events.jsonl` and expose basic usage/duration.
- [ ] Change stock evals to deterministic-first and parse report artifacts directly.
- [ ] Make `pm_planner_dispatcher` install/declare its `inner_outer_eval` dependency.
- [ ] Rewrite and test the Agent Skill against a clean generated repo.
- [ ] Remove all multi-worker claims unless/until they are true.
- [ ] Pin/test the pre-1.0 team-harness compatibility range and check eval-banana availability.
- [ ] Add a clean-install packaging/template smoke job in CI.

### Documentation and proof

- [ ] Publish the end-to-end tutorial with expected output.
- [ ] Publish architecture/state-machine, workflow authoring, troubleshooting, cost, and security guides.
- [ ] Create one demo repo with a pinned goal, deterministic checks, a redacted successful session bundle, and a short video/GIF.
- [ ] Publish at least three dogfood case studies: small task, crash/resume, and multi-WP parent/child; include elapsed time, tokens/cost coverage, failures, and human interventions.
- [ ] Run the UGM WP-0.4 pilot and publish the honest trace even if it fails.
- [ ] Document supported OS/Python/CLI/model combinations.

### Project/community surface

- [ ] Add `CONTRIBUTING.md`, `SECURITY.md`, code of conduct, support policy, issue/bug templates, and a minimal public roadmap.
- [ ] Enable GitHub Discussions or name one support channel; define response expectations suitable for one maintainer.
- [ ] Label good first issues only after contributor setup is reproducible.
- [ ] Add privacy/telemetry policy (prefer no telemetry by default).
- [ ] Automate stale docs/model-string checks and link validation.

## Suggested sequencing and version labels

1. **Hardening release (0.3.x Alpha):** state stack, typed outcomes, events, deterministic eval policy, PM template, skill correction.
2. **Dogfood cycle:** run several bounded real projects, especially UGM WP-0.4; fix failure modes and collect measured cost/time data.
3. **Documentation/demo release:** tutorial, architecture, workflow authoring, troubleshooting, security/cost, demo artifacts.
4. **0.4.0 Beta:** only after active-session migrations work, the event/outcome schemas are versioned, the compatibility matrix is CI-tested, and several multi-day resumes have succeeded. Beta means config/CLI/session changes get deprecations or migrators.
5. **Public “official launch”:** launch the Beta, not 1.0. Be explicit that it is a single-checkout, single-loopy-worker system with internal team-harness parallelism.
6. **1.0:** after at least a few months of Beta use, no known state-loss bugs, documented recovery/cancellation, stable public contracts, bounded spend controls, and a repeatable benchmark/case-study suite. Parallel loopy workers are not required for 1.0.

# Q4. State of team-harness and eval-banana

## team-harness 0.2.10

### Architecture and strengths

The installed v0.2.10 package is about 8,673 Python LOC. I inspected that installed code directly. For test breadth I also used the nearby source checkout: it still declares 0.2.10 but contains a small unreleased Antigravity change, so its test count is evidence about the current development line rather than a byte-for-byte claim about the published wheel. The architecture is intelligible:

- `harness.py` owns lifecycle/config/client/tool registration/finalization and exposes a small SDK.
- `coordinator/loop.py` implements the tool-call conversation, retries, usage/context tracking, and compaction.
- `agents/template.py`, `registry.py`, and `spawner.py` turn configured agent types into argv/env and launch them with `asyncio.create_subprocess_exec()`.
- `tools/agent_tools.py` exposes spawn/status/read/wait/kill and records worker metadata.
- `tracking/` writes run turns, agent sessions, failures, and resumability metadata.
- Per-run tool-binding closures isolate cursor state across concurrent SDK runs.

Specific strengths in the actual code:

- Worker subprocesses use argument arrays rather than a shell, avoiding a large injection class in the worker launch path.
- Worker labels are validated and resolved under the session output directory, a good fix documented in 0.2.10.
- Output reads are bounded, incremental cursor state is shared with wait snapshots, and worker stdout/stderr are preserved.
- Codex/Gemini/Claude session IDs are captured for explicit resume; unsupported resume is represented in the manifest.
- Coordinator 429/5xx/network failures get exponential backoff and structured `RunFailureRecord` diagnostics.
- Shutdown finalizes logs and writes a worker-session manifest even on exceptions.
- The patience policy, API failure classifier, context tracker, and prompt warnings show operational experience rather than a toy orchestration loop.
- The development source suite is substantial: 448 tests ran in the available environment. 446 passed; two Rich styling tests failed because expected bold ANSI codes were absent. Those are UI-only failures, but a release suite should still be green under declared dependency ranges.

This is solid Alpha engineering. It is considerably more mature than its 8.7k LOC size implies.

### Critical failure modes

#### Normal return is not successful execution

`TeamHarness.run()` returns `TeamHarnessResult` if the coordinator loop ends without `run_log.error`. Failed workers remain summaries; they do not automatically fail the harness. A coordinator can synthesize a final answer after a failed worker, or simply decide it has enough. That flexibility is reasonable for an orchestrator, but consumers must not interpret it as task acceptance. Loopy-loop currently does.

The result needs an explicit orchestration outcome and policy fields such as `all_required_workers_terminal`, `failed_agents`, `killed_agents`, `empty_final`, and `coordinator_declared_complete`. Better yet, let the caller supply an acceptance callback/required artifact contract.

#### Premature coordinator finalization kills live work

The coordinator run ends whenever the model emits an assistant response with no tool calls. `_finalize_run()` then waits only `shutdown_timeout_s` (default 10 seconds) for running workers before terminating them. This conflicts with the system prompt's expectation that Codex tasks commonly take 20–45 minutes and should not be killed before 600 seconds. The minimum lifetime protects the model-visible `kill_agent` tool, but not finalization cleanup.

A single premature final answer can therefore kill useful workers and still return a normal result. Enforce “no final while required agents run” in code, or return an explicit incomplete/error outcome. Prompt discipline is insufficient.

#### No run budget or termination bound

`coordinator.loop.run()` is an unbounded `while True` until the model stops making tool calls. There is no maximum coordinator turn count, wall-clock deadline, spawn count, concurrency cap, token budget, or cost budget. `bash` has a 120-second command timeout, but the orchestration run and worker agents do not have equivalent caller policy. One confused model can loop, overspawn, or wait indefinitely.

These controls belong in team-harness so every caller benefits; loopy-loop's outer `max_turns` does not bound cost inside one harness iteration.

#### Headless auto-compaction appears ineffective during a tool chain

`_should_compact()` requires the last message role to be `user`. In a headless single-shot run, the initial user task is followed by assistant tool calls and `tool` results. Subsequent turns normally end in `tool`, so the compaction predicate will not run as the tool chain grows. Compaction is well tested for interactive/new-user boundaries, but the loopy-loop workload is a long single user turn. That path needs a dedicated test and a compaction strategy that can preserve an in-flight tool-call transcript safely.

#### CLI integrations are a volatile compatibility surface

Agent templates hard-code flags, session event shapes, model injection, and auth environment behavior for multiple independently changing CLIs. The abstraction is thoughtfully implemented, but unit tests mostly mock subprocesses and the one named integration test uses a fake coordinator API. A scheduled live smoke matrix is necessary. Missing binaries currently warn during template validation; a coordinator can still choose one and fail at runtime.

#### Security is intentionally broad

The built-in Codex/Claude/Gemini templates use sandbox/approval bypass modes. Coordinator shell and filesystem tools are also unrestricted, and a spawn `cwd` is resolved but not confined to the configured project. The system prompt says the coordinator is not an implementer, but tools do not enforce that boundary. This is acceptable for a trusted local power tool, not for untrusted repos or multi-tenant use. Documentation and optional workspace confinement are required.

#### Tracking durability and code complexity

`RunLogWriter._flush()` rewrites `run.json` directly rather than temp-file/replace, so a process or disk failure can corrupt the primary run log. `agent_tools.py` is 1,319 lines and contains both legacy global tool functions and near-duplicate per-run closure implementations; the duplicate `except asyncio.TimeoutError` in the global `wait_for_agents()` path is a small sign of drift. Refactoring around a per-run tool context would reduce risk.

#### Cost visibility is incomplete

Coordinator usage is recorded per turn and context limits are handled thoughtfully, but `TeamHarnessResult` exposes no usage aggregate and no cost. Worker usage is not normalized. For UGM, where every LLM worker must have an append-only ledger (D52) and budgets must be enforced, this is insufficient without additional instrumentation.

### Test and maturity verdict

The test volume is a genuine strength: config merging, command rendering, session capture, retries, compaction, output cursors, shutdown, manifests, and error rendering all receive attention. However:

- tests are mostly unit/mocked integration;
- no cross-provider live CLI contract is demonstrated by the suite I ran;
- pyright is `basic` with many report categories disabled;
- dependency lower bounds have no upper bounds, and the two Rich failures demonstrate drift risk;
- v0.2.0 intentionally shipped breaking SDK/env changes, appropriate for Alpha but a warning to loopy-loop's unbounded dependency spec.

**Is it solid enough for serious UGM work?** As a supervised delegation engine inside one bounded WP, yes. As an authority that says a WP is complete, no. Use it to obtain plans, diffs, reviews, and artifacts; use UGM's deterministic contracts and human decisions to accept them. Add run budgets and incomplete-worker enforcement before a multi-day autonomous pilot.

## eval-banana 0.3.0

### Architecture and strengths

Eval-banana is deliberately compact: about 2,030 source LOC. The v0.3.0 source suite has 115 tests, all of which passed in the available Python 3.13 environment. It has a clean pipeline:

1. discover YAML checks;
2. parse strict discriminated Pydantic definitions (`extra="forbid"`, `schema_version: 1`);
3. select deterministic or harness-judge runner;
4. normalize binary results;
5. calculate threshold/error-aware `run_passed`;
6. write human and machine reports plus per-check stdout/stderr.

Good decisions include fail-fast validation when a selected judge lacks a configured harness, path-specific load errors, duplicate-ID detection, explicit `error` distinct from `failed`, and making any errored check block `run_passed` even if the numeric threshold is met. The last-valid-JSON parser handles chatty agent stdout better than expecting a pristine response.

For deterministic checks and reporting, it is a useful lightweight framework. It can serve UGM as a runner/report format, though UGM's pytest-native golden-set harness will be richer than a generic binary check list.

### Judge reliability and pitfalls

`harness_judge` is not reliable enough to be the sole stop condition of a long-running coding loop.

#### Non-determinism and no calibration

Each check gets one binary verdict from one agent invocation. There is no seed/temperature surface, repetition, majority vote, confidence, disagreement record, calibration set, or flakiness history. Binary scoring is easier to reason about than a 1–5 scale, but it does not make a single stochastic judgment deterministic.

For release gating, run qualitative checks multiple times or across model families and record disagreement. Calibrate each judge prompt against human-labeled pass/fail examples before trusting it.

#### The judge can modify what it judges

The coding agent runs in the project root with built-in approval/sandbox bypass flags. The prompt asks it to read and evaluate, but no code makes the workspace read-only. A judge can modify implementation, tests, or eval definitions before returning a verdict. Even accidental formatter/fix behavior compromises the measurement.

Run judges in a read-only snapshot/container or detached worktree and verify the tree hash before/after. Network and secrets should be minimized. This is essential if the verdict can stop an autonomous loop.

#### Prompt injection and gaming

The agent reads repository files under natural-language instructions. Repository content can instruct it to ignore the evaluator or emit a passing JSON object. More subtly, the same implementation agent can weaken checks or tailor prose to the judge. The loopy-loop stock eval reviewer magnifies this by letting an agent author the high-level checks that later decide completion.

Keep required check definitions outside the implementation's writable scope or require independent review of check changes. Prefer behavioural tests over judging self-authored claims. Use a different model family for judging and include adversarial calibration cases.

#### Exit-code handling is too permissive

`run_harness_judge_check()` parses stdout even when the agent exits non-zero; the test `test_non_zero_exit_with_valid_json_still_passes` explicitly codifies that a valid `score: 1` can pass despite exit code 1. This salvages useful final output, but for a gate it can turn a crashed agent's stale/partial verdict into success. A non-zero exit should be at least `inconclusive/error` by default, with an opt-in salvage mode.

#### Judge provenance is incomplete

The check result records model, agent type, raw stdout, and exit code, but not the exact rendered prompt hash, agent CLI version, template/config hash, repository commit/tree hash, environment identity, or judge repetition. Those are necessary to reproduce and audit a terminal decision.

#### Dead/stale configuration and documentation

v0.3.0 removed `target_paths`, instructing the real coding agent to find files named in `instructions`. `Config.llm_max_input_chars` remains parsed and documented as a per-target-file truncation control, but it is not used anywhere in the judge runner. The `harness_judge` docstring also says it builds a prompt from target-file contents, while `_build_judge_prompt()` contains only description and instructions. This is concrete API/docs drift.

#### Execution limits

Judge subprocesses have a hard-coded 300-second timeout, not a per-check/configurable value. Deterministic checks have no timeout at all; one hung script blocks the entire sequential run. Checks run serially, so a qualitative suite can be slow and expensive. Add per-check timeouts, a safe global limit, and controlled parallelism for independent read-only checks.

#### Equal weighting and thresholds

Every check has equal one-point weight. A `pass_threshold` below 1.0 can allow a critical check to fail as long as enough minor checks pass. Introduce required/critical checks or named suites; terminal loop completion should require all mandatory contracts regardless of aggregate threshold.

### Recommended role in loopy-loop and UGM

Use eval-banana for:

- orchestrating deterministic scripts and normalizing reports;
- lightweight qualitative smoke checks;
- preserving stdout/stderr and a simple pass/fail artifact;
- developer feedback and regression trends.

Do not use one harness-judge pass as a stop switch. For UGM, acceptance should be layered:

1. required pytest/import/migration/schema/golden-set metrics pass;
2. required eval-banana deterministic checks pass;
3. qualitative judge checks, if any, pass under an independent, read-only, calibrated policy;
4. human resolves explicit architecture/model/infra/golden-label gates;
5. the loopy-loop verifier derives the stop artifact from those reports.

## Foundation risk across the three tools

The stack has good conceptual separation:

- loopy-loop supplies long-horizon durable workflow/session state;
- team-harness supplies short-horizon model/worker orchestration;
- eval-banana supplies check discovery and report artifacts;
- git and project-owned tests remain the durable implementation evidence.

The coupling is nevertheless risky. All three are Alpha, maintained by one person, share hard-coded model/CLI assumptions, and can evolve together without an external compatibility constraint. Loopy-loop currently depends on `team-harness>=0.2.10` with no upper bound even though team-harness has already made breaking pre-1.0 changes. Eval-banana is conventionally required by the recommended template but not installed or validated as a dependency. Integration tests do not prove a clean installed trio executes a full template with real CLIs.

Treat the trio as one release train for now: compatibility matrix, pinned ranges, end-to-end clean-environment test, shared outcome/event vocabulary, and coordinated release notes. Decouple later when the contracts have survived real use.

# Bottom line

The bet is sound in its restrained form: fresh agent contexts plus durable repository state, explicit workflows, git, and independently verified artifacts are a credible way to extend useful coding work beyond one chat. The biggest risk is **false closure**—agents and harnesses producing plausible completion prose, altered evals, or mechanically successful runs that the system promotes to accepted work. It is not primarily the single-worker ceiling or context-window size. The single highest-leverage next move is to run one UGM Phase 0 work package—preferably WP-0.4—through a hardened, single-worker pilot whose acceptance tests are frozen and deterministic, while recording every transition, failure, token/cost observation, human intervention, and final diff. Use that evidence to define the shared outcome contract across loopy-loop, team-harness, and eval-banana before investing in parallelism or a polished dashboard.
