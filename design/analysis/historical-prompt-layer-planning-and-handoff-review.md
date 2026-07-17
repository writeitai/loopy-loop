# Historical prompt review: layer planning, eval autonomy, goal granularity, and handoff

**Status:** architecture analysis; non-binding working notes

**Date:** 2026-07-17

**Scope:** the historical and current `inner_outer_eval` and
`pm_planner_dispatcher` prompt contracts, the live Ultimate Memory double-loop
session, and the requirement that `inner_outer_eval` remain independently
executable as a one-layer system

**Accepted resolution:** The binding design chose an explicit protocol v3 for
new sessions so live v2 sessions keep their frozen authority. References below
to “changing v2” or removing a v2 prerequisite describe the semantic change
that v3 must implement; they do not authorize reinterpreting an existing v2
session in place.

## Executive conclusion

The current run did not independently choose the wrong abstraction level. It
faithfully followed prompts that told the PM layer to dispatch one work package
or a similarly narrow maintenance/closeout item, then gave the child delivery
layer a nearly leaf-level assignment. That left too little meaningful planning
for the child's `outer` role to do.

The original one-layer prompts were clearer about the important semantic split:

- `outer` owned the durable high-level plan, decomposition, task lifecycle, and
  acceptance for its session;
- `inner` implemented exactly one available leaf and returned it for review;
- the plan and leaf specifications had named, visible paths and stable status
  transitions; and
- the same workflow could take a broad root goal and drive it to completion
  without a PM parent.

The recursive v2 work correctly strengthened session identity, absolute paths,
parent/child provenance, eval provenance, and trace separation. In compressing
the old prompts, however, it made the semantic planning contract implicit and
also overcorrected evaluation ownership. The stock `inner_outer_eval` outer
still claims planning ownership, while its inner is also told to create a plan
when one is absent. Its workflow contract does not declare a plan, task tree,
eval-status index, or semantic handoff as accountable state.

V2 completion is now unnecessarily eval-gated. `eval_runner` is the only role
allowed to publish `goal_met`, and the engine requires a passing same-attempt
goal-check projection and eval receipt. That is the wrong semantic boundary.
The persistent layer orchestrator—`outer` for `inner_outer_eval`, `planner` for
`pm_planner_dispatcher`—should decide when its goal is complete. Evals are
optional, provenance-rich observations that the orchestrator may run directly,
return so a scheduled eval role can produce for its next attempt, or decide are
unnecessary. “Optional” here means optional to the Loopy protocol; a repository
goal can still instruct the orchestrator to run a particular prepared eval
before declaring completion.

There is also a concrete upward-communication defect. The coordinator already
looks for `project_state/handoff.json` and places its reference in the factual
parent-side child outcome, but no packaged prompt or workflow contract requires
any role to create that file. The live Ultimate Memory child therefore has
accepted work, decisions, eval evidence, and delivery evidence, but no standard
semantic result that tells its parent what was achieved, what was learned, what
remains, and what disposition it recommends.

The recommended direction is to give every durable session layer the same
small, obvious semantic spine: a layer plan, current state, task details,
decisions, accepted-work ledger, eval-status summary, and semantic handoff. The
engine should scaffold and expose absolute paths to those artifacts and validate
their structural provenance. Agents should continue to decide the plan and its
meaning. This preserves D8: the engine makes state visible and accountable; it
does not parse the plan to decide what work is wise or prevent agents from
changing the repository.

Most importantly, this contract must be topology-neutral. A root
`inner_outer_eval` session and the same workflow used as a child should have the
same planning, completion, and optional-evidence semantics. Nesting adds a
parent that consumes the session's outcome; it must not turn the workflow into
a different species.

## Terminology used here

The distinction in D10 remains essential:

| Term | Meaning |
| --- | --- |
| Durable session layer | One loopy session with its own goal, plan, state, decisions, evals, attempts, and optional child |
| Workflow role | A scheduled role inside that session, such as `planner`, `outer`, `inner`, or `eval_runner` |
| Harness delegate | A dynamic, attempt-local agent spawned by a Team Harness coordinator |

An `inner` workflow role is not a nested durable loop. A spawned Codex or Claude
agent is also not a durable child session. Only a typed child-session request
creates another durable layer.

## Evidence reviewed

This review compared:

- the first packaged `inner_outer_eval` prompt at loopy-loop commit `17f72b8`;
- the mature pre-v2 prompt lineage through commits `0f662b2`, `ff8fa1c`,
  `9efdb14`, `bc6d9ee`, `f6e2fa5`, and `b1e4fe8`;
- the first packaged PM workflow at commit `8259343`;
- the recursive layer contract introduced at commit `01a560d` and refined on
  current `main`;
- Ultimate Memory's original standalone whole-roadmap setup at commit
  `04a480e` and its double-loop conversion at commit `7a65bfe`;
- current stock workflow contracts and prompts under
  `src/loopy_loop/templates/`;
- `build_attempt_assignment()` in `src/loopy_loop/assignments.py`;
- `_terminal_evidence_projection()` and child-outcome publication in
  `src/loopy_loop/coordinator_app.py`;
- the workflow-owned state and child-outcome descriptions in
  `docs/session-layout.md`; and
- the live Ultimate Memory parent session
  `20260717_025626_91f946163870_495df8c0` and child session
  `20260717_044051_85af1686ad7c_78755280`.

The live session is evidence of the contract's consequences, not a claim that
one model's private reasoning should be exposed. The desired observability is
the plan, decisions, accepted facts, and handoffs that affect future work—not
hidden chain-of-thought.

## Follow-up correction: evals inform the orchestrator; they do not own completion

### The history confirms the distinction

The first `inner_outer_eval` template at `17f72b8` did include outer, inner,
eval reviewer, and eval runner. Evals were sparse scheduled roles: the reviewer
ran at startup and after every ten successful inner runs; the runner could not
run before iteration ten and followed the reviewer every ten successful inner
runs. At first, its `goal_check.json` was the semantic stop signal.

Commit `d84942d` later gave outer a second path to stop when accepted work
satisfied the full goal. That created ambiguous dual authority: outer could
decide from accepted implementation evidence, while eval runner could decide
from its check run. The correct repair is one completion owner, but it need not
be the evaluator.

The first `pm_planner_dispatcher` template at `8259343` had exactly two roles:
`planner` and `dispatcher`. Planner owned the full-goal stop decision. Child
eval and `goal_check` artifacts were review evidence “when present,” and
dispatcher explicitly said planner owned goal-level stop. The user's memory is
therefore correct.

Commit `01a560d` added parent eval reviewer/runner roles, changed both stock
contracts so `eval_runner` alone owned `goal_met`, and made an eval receipt a
protocol-v2 prerequisite for success. It also made evals much more frequent:
the stock reviewer runs before ordinary work and the reviewer/runner pair recurs
after every three successful inner or planner runs.

### The current mandate is engine behavior, not merely prompt advice

Several independent mechanisms now enforce evaluation:

1. `ControlSignal.validate_stop_reason()` in `src/loopy_loop/models.py` rejects
   every v2 `goal_met` without `eval_receipt_ref`.
2. `CoordinatorService._validate_v2_control()` requires the producer to equal
   `contract.eval.goal_control_role`, requires a passing same-attempt
   `goal_check.json`, and validates a matching passing same-attempt eval receipt
   and report/git provenance.
3. Both packaged contracts assign that role to `eval_runner`; outer and planner
   prompts explicitly forbid successful control.
4. `emits_goal_check` turns missing or invalid evaluator output into a failed
   iteration and can eventually terminate the session as `goal_check_broken`.
5. The PM template now schedules parent eval roles even though each child has
   its own eval capability and the planner can run program-level prepared evals
   directly when useful.

This is stronger than “evaluation is available.” It makes one evaluation shape
the mandatory arbiter of semantic completion and can turn a broken advisory
observation into a terminal failure of otherwise recoverable orchestration.

### Revised ownership

Each workflow set should name one durable orchestration role whose continuity
lives in session state rather than a persistent model process:

- `inner_outer_eval`: `outer` owns the layer plan, leaf acceptance, handoff,
  and completion decision;
- `pm_planner_dispatcher`: `planner` owns the program plan, child acceptance,
  final evidence review, handoff, and completion decision; and
- future recursive layers: the contract names the analogous orchestration role
  rather than assuming a role whose name contains `eval`.

The workflow schema should represent this directly. Completion ownership does
not belong under `eval.goal_control_role`; use a top-level orchestration or
completion owner. The eval sub-contract then names only optional author/runner
roles. V2 `goal_met` keeps exact current session/workflow/attempt identity and a
nonblank rationale, while eval receipt references become optional evidence.

Eval reviewer/runner roles may still be useful in `inner_outer_eval`. They
author and run independent observations on a schedule, update compact eval
state, and publish receipts for a later outer attempt to consume. They never
publish terminal control. Harness coordinators remain attempt-local: outer does
not stay alive waiting across scheduled roles. Its current attempt returns, the
scheduler runs any due evidence role, and a later outer attempt reads the
durable result. Outer can:

- run an eval itself or delegate it when immediate evidence is useful;
- avoid duplicating that work when schedule context says an eval role is due;
- return so the scheduled role can run, then use its result in a later attempt;
- repair work after a meaningful failure;
- decide that a check is stale or inapplicable and record why; or
- conclude the goal from other evidence without running an eval.

Eval provenance remains strict when an eval is accepted or cited as trustworthy
evidence. Provider, model, check definitions, report hashes, git subject, and
producer identity should still be recorded. Those facts make the observation
trustworthy; they do not make it a hard terminal gate. Unrelated malformed eval
output remains a diagnostic and cannot block completion.

For UGM, the PM workflow should return to planner plus dispatcher. Its goal and
planner prompt should point at the prepared final checks under
`plan/implementation_evals/` and instruct planner to run them near overall
completion, directly or through a dynamic harness delegate. The result is
important program evidence because UGM's goal says so, not because Loopy refuses
to accept `goal_met` without a particular receipt.

## What the original one-layer contract got right

### 1. It named one durable planning owner

The earliest packaged outer prompt opened with a plain contract: outer owns
high-level planning, the overall plan, current state, and review; inner
implements leaf tasks. The matching inner prompt said it implements exactly one
available leaf and must not invent a broad plan when no leaf exists.

That division remained clear in the mature pre-v2 prompts:

- outer created and refined the next useful slice of the plan;
- outer wrote acceptance criteria for available leaves;
- inner selected one available or failed leaf;
- inner could move only that leaf through implementation states; and
- outer alone accepted the leaf, updated dependencies, and added it to the
  finished ledger.

This did not constrain the Team Harness coordinator's delegation. Inner could
still choose a dynamic team for its selected leaf. The contract constrained
durable session ownership, not the shape of the ephemeral agent graph.

### 2. It made the plan inspectable

The old state layout explicitly named:

```text
project_state/
├── current_state.md
├── memory.md
├── what_we_have.md
├── decisions.md
├── eval_results.md
├── finished.md
└── what_we_should_do/
    ├── plan.md
    └── tasks/<task-id>/README.md
```

The root plan stayed concise. Detailed context and acceptance criteria lived in
task files. Stable task markers made the handoff visible:

```text
available
in progress
inner complete, waiting for outer
failed / needs repair
accepted
```

The exact vocabulary can improve, but the lifecycle itself is valuable. A
reviewer could tell what outer intended before implementation, what inner was
doing, and what outer had actually accepted.

### 3. It used progressive disclosure rather than duplicating the whole roadmap

The historical prompt did not require fully expanding every future task. It
kept the root plan readable, expanded only the next useful slice, and put
detailed criteria in leaf files. That is compatible with a repository roadmap
remaining authoritative.

A session plan can be a compact execution projection containing stable IDs,
status, dependencies, current selection, and source references. It need not
copy the roadmap's prose or become a competing source of truth.

### 4. It worked with a broad standalone goal

The first template goal was a complete product outcome, not a preselected leaf.
Ultimate Memory's first one-layer setup similarly gave `inner_outer_eval` the
whole phase 0–8 roadmap and was configured for outer to decompose that goal and
inner to implement leaves.

This shows that decomposition is part of the intended capability of
`inner_outer_eval`, not something that exists only in a PM parent.

## What should not be restored from the old prompts

The historical prompts also accumulated substantial material that should stay
retired:

- a fixed mandatory Codex/Claude/Gemini team recipe;
- provider-specific orchestration boilerplate repeated in every role;
- an outer role that sometimes orchestrated implementation despite assigning
  implementation to inner;
- dual successful-control authority split between outer and eval runner; one
  declared orchestration role should own completion instead;
- `waiting_for_human`, contrary to D5;
- v1 control payloads and repo-root-relative state-path assumptions;
- truncation of the old user-update inbox; and
- stock agent-authored deterministic checks, contrary to D4.

The lesson is to recover the concise planning and ownership protocol, not to
restore the old prompt wholesale.

## Confirmed gaps in the current contract

### F1. Durable planning ownership is ambiguous

The stock outer prompt says it owns planning and `what_we_should_do/`. The stock
inner prompt then says:

> If the state does not yet have a useful plan, create the smallest maintainable
> plan needed for this attempt.

The stock `contract.yaml` compounds the ambiguity: it declares
`current_state.md`, `decisions/`, `finished.md`, and eval directories, but does
not declare `what_we_should_do/`, a plan file, or task specifications.

This creates two possible planning owners:

- outer can maintain durable cross-attempt decomposition; or
- inner can create an attempt-driven plan when it feels one is missing.

The second behavior is useful as tactical planning but wrong as durable layer
ownership. Inner should be free to make an execution checklist for the
selected leaf. It should not silently become the session backlog owner.

### F2. The plan path is inconsistent and not an Assignment-level contract

Current PM sessions use `project_state/work_items.md`. The Ultimate Memory child
uses `project_state/what_we_should_do/plan.md`. The stock delivery contract does
not require either exact path. `build_attempt_assignment()` exposes the broad
absolute `project_state` directory but no named absolute layer-plan, task-tree,
eval-status, or handoff path. Session creation in `src/loopy_loop/sessions.py`
creates the project-state directory and `finished.md`, but does not scaffold a
plan, current-state file, eval index, or handoff.

The live child eventually created
`project_state/what_we_should_do/plan.md`, but only after delivery work had
already completed. It is mostly a retrospective list of accepted steps plus the
remaining eval step. It did not show the child's intended decomposition before
the work happened. The fact that a capable agent later created a useful file
does not make the file a reliable protocol surface.

### F3. The current Ultimate Memory goal deliberately dispatches at leaf scale

Ultimate Memory's current root goal says the PM selects one work package at a
time and that each child implements exactly one WP or explicitly coupled WP
group. Its planner and dispatcher prompts reinforce this rule. The specific
child goal—“Execute exactly one evidence-backed `PLAN-RECONCILIATION` for Phase
0 WP-0.1”—is therefore the expected result of the prompt, not agent drift.

At that granularity, the child outer can only break one prescribed edit into
minor execution steps. Its durable planning role is largely ceremonial.

The repository phase files should remain planning authorities, but that does
not imply that one repository WP must equal one durable child session. A parent
can select a coherent milestone and reference the relevant phase/WP authority;
the child outer can then decide which leaves and PRs are needed to achieve it.

### F4. The engine has an orphaned semantic-handoff reference, not a producer contract

When a child becomes terminal, the coordinator writes
`child_outcomes/<request-id>.json`. That factual record correctly separates
child lifecycle evidence from parent acceptance.

`CoordinatorService._terminal_evidence_projection()` contains an optional
discovery hook for:

```text
project_state/handoff.json
```

and, when present, places a logical reference to it in
`child_outcome.evidence_refs.handoff`.

However, no current packaged `inner_outer_eval` role is required to create the
file; it is absent from its `contract.yaml`; no exact absolute handoff path is
provided in the Assignment; and the live Ultimate Memory child has no such
file. At review time the child had written successful control intent, but its
engine state and parent edge were still `running` because the coordinator had
been deliberately stopped before finalizing the transition; no child outcome
had been published. When that transition is finalized, the current projection
will contain `handoff: null` unless the artifact is repaired first. This is
also a useful distinction: workflow control intent and committed engine
lifecycle are not the same fact.

The parent can reconstruct meaning by reading `current_state.md`,
`finished.md`, decisions, eval receipts, delivery receipts, and perhaps traces.
That is recovery by archaeology, not a solid inter-layer contract.

There is a related cross-attempt delivery-lineage gap. The current
`_delivery_ref_for_attempt()` accepts only a delivery receipt whose attempt ID
equals the terminal control producer attempt. Normal delivery is performed by
an earlier inner/outer attempt, while successful terminal control is produced
by a later `eval_runner` attempt. The live delivery receipt and terminal eval
attempt have exactly that shape, so the projected delivery reference would
also be null. A correct terminal outcome must bind delivery through the
outer-accepted work/handoff lineage, not assume delivery and terminal eval were
performed by the same attempt.

### F5. Ultimate Memory's customized delivery workflow is not standalone-safe

The stock template mostly refers to “this session's scoped goal,” which is
correct at any depth. Ultimate Memory's customized copy instead assumes:

- this layer owns one dispatched WP or maintenance item;
- the repo-root goal belongs to a PM parent;
- an accepted child request and frozen parent inputs exist; and
- eval must never substitute the PM/root goal for “this child.”

Those statements are false when the same workflow set is launched as a root
one-layer session. In particular, the customized eval reviewer expects an
accepted request and dispatcher-selected curated inventory that a root session
does not have.

This is not merely awkward wording. It makes the workflow's evidence inputs
conditional on a parent while its declared `child_interface: none` correctly
says the workflow itself needs no durable child.

### F6. Suspended parent projections are expected, but inspection is poor

The live parent `current_state.md` and `child_sessions.md` remained stale while
the child ran, whereas engine-owned `children.json` correctly showed the live
child. With the deliberate depth-first single-worker model, the suspended
parent agent cannot continuously rewrite its semantic projection. That is not
evidence that the engine lost the child.

The inspection problem is still real: a reviewer opening the parent session
does not get one obvious route to the active layer's goal, plan, current state,
and eventual handoff. The remedy is a topology-aware status/index view that
links canonical artifacts for every session on the active stack—not a second
concurrent parent worker and not engine parsing of plan semantics.

## Required standalone invariant for `inner_outer_eval`

`inner_outer_eval` must be runnable both as:

1. a root, one-layer system with `parent_session_id: null`; and
2. a child delivery session inside a double- or deeper-loop tree.

The following invariants should hold in both cases:

1. The session's own immutable `goal_contract` is always authoritative.
2. The workflow never requires a parent, accepted child request, PM backlog, or
   parent-owned curated-check selection to exist.
3. If an accepted request and frozen inputs do exist, they provide scoped
   origin context; they do not replace the session goal.
4. Outer bootstraps and maintains the durable layer plan before ordinary inner
   work.
5. Inner consumes one selected leaf. It may make an attempt-local tactical
   checklist and dynamically delegate, but it does not own broad layer
   decomposition.
6. Outer reviews each leaf and alone marks it accepted in the layer plan and
   finished ledger.
7. Eval reviewer and eval runner, when scheduled or invoked, evaluate this same
   session goal and publish evidence for outer. Outer remains the only
   successful terminal-control owner.
8. For a normal completion, outer publishes the same semantic handoff artifact.
   For a child, the engine exposes it to the parent. For a root,
   status/inspection presents it with terminal control and any eval facts as the
   operator-facing result. Missing handoff is visible incompleteness for later
   review, not an engine veto over the orchestrator's completion decision. A
   crash, failure-cap stop, or legitimate D5 blocker may also terminate without
   an outer-authored handoff.
9. `child_interface: none` remains valid. Dynamic Team Harness delegates are
   attempt-local and do not require another durable session.
10. Prompt language is topology-neutral: “if this session has a parent,” never
    “the PM parent” as an unconditional fact.

This invariant deserves an end-to-end contract test. Existing initialization
and parent-dispatch tests do not prove that a fresh root `inner_outer_eval`
session can bootstrap a plan, implement and accept leaves, decide completion of
its own broad goal with or without eval evidence, publish its outcome, and close
successfully without any parent artifacts.

## Recommended semantic state contract

Every durable session should expose the same obvious state surface, regardless
of depth or workflow-set names:

```text
project_state/
├── plan.md
├── tasks/
│   └── <stable-item-id>.md
├── current_state.md
├── memory.md                 # optional, concise durable facts only
├── decisions/
├── eval_state.md
├── finished.md
└── handoff.json
```

`plan.md` is the layer's execution projection. It should contain or link:

- the layer goal reference and hash, without restating a competing goal;
- stable item IDs and outcome-oriented descriptions;
- authority/source references into repository plans and designs;
- dependencies and observable acceptance criteria;
- status and the currently selected leaf;
- enough near-term lookahead to understand the intended direction; and
- the next expected role/action.

It should not duplicate an entire repository roadmap. Detail belongs in
`tasks/<stable-item-id>.md`, and speculative distant work can remain unexpanded.

`eval_state.md` is a compact human-readable evidence index maintained by eval
roles or by the orchestrator when it runs an eval directly. It links the active
check inventory, latest validation/run/receipt, important coverage gaps, and
next eval action. It does not replace canonical checks or receipts and does not
turn an eval result into terminal authority.

`handoff.json` is the layer's semantic outcome. A minimum useful shape would
identify:

- session and goal identity;
- the plan and accepted-work ledger references;
- achieved outcomes and accepted item IDs;
- important findings and decisions;
- unachieved scope and remaining risks;
- PR, commit, CI, eval, and delivery evidence references when present; and
- the producing role/attempt plus a recommended consumer disposition such as
  `accept`, `rework`, or `reroute`.

The recommendation is advice, not parent acceptance. The parent planner still
reviews the child goal, repository state, eval evidence, and handoff before
publishing a separate `parent_acceptance` receipt.

During a running session, outer may atomically update `handoff.json` as a
rolling semantic draft. At terminalization, the engine should snapshot or hash
the exact observed bytes into the immutable terminal session outcome. The
parent then consumes that bound terminal version rather than an unversioned file
that could later drift. If no handoff exists, the outcome records that factual
absence without blocking the orchestrator's decision.

The handoff alone is not the complete terminal result. Terminal control and any
scheduled eval observation may be produced in different attempts. The engine
should produce a compact terminal session-outcome projection for every
session—root or child—that combines lifecycle/control facts, the semantic
handoff reference, optional eval and git evidence, validated cross-attempt
delivery lineage, usage, trace reference, and explicit completeness flags. A
parent's `child_outcome` can link or project that same record; root status can
display it directly. This makes standalone and nested completion the same
contract without making eval presence a completeness requirement.

The semantic handoff also must not manufacture a new terminal state. If a
design-conflict report is a valid alternate child outcome, that alternative
must be explicit in the child goal contract. Otherwise an unfinished task is
rework, and a genuinely terminal human-only blocker remains D5's rare
`unresolvable_error` path.

This gives a cold reviewer a different, appropriate view at each level:

- the PM parent's `plan.md` shows which program outcomes or milestones it
  expects to complete and why one is selected;
- the delivery session's `plan.md` shows how its outer role decomposed that
  outcome into leaves and which leaves are accepted or next;
- `eval_state.md` shows what the eval roles intend to judge, what has run, and
  what gap remains; and
- attempt traces show the inner coordinator's dynamic delegate graph and
  tactical execution details without turning those transient choices into the
  durable layer plan.

## Role ownership within one session

| Artifact or decision | Accountable owner | Allowed contribution |
| --- | --- | --- |
| Immutable layer goal | Engine/session creation | Roles read it; none rewrites it |
| Durable layer decomposition | `outer` or PM `planner` | Inner may propose follow-ups in its handoff |
| Selected-leaf lifecycle | `outer`/`planner` accountable | Inner may mark only its selected leaf in progress and ready for review |
| Tactical execution checklist | `inner` | Attempt-local; promote only durable facts or decisions |
| Leaf implementation and delegate integration | `inner` | Dynamic delegates report to inner |
| Leaf/child acceptance | `outer` or PM `planner` | Review delegates may advise but not decide |
| Eval check policy/inventory, when configured | `eval_reviewer` | Delegates may inspect evidence |
| Eval run/receipt evidence, when configured or directly invoked | `eval_runner` or the orchestrator | Evidence only; never owns terminal control |
| Session completion and `goal_met` | `outer` or PM `planner` | May cite eval evidence but does not require it at protocol level |
| Semantic layer handoff | `outer` or PM `planner` | Normal upward/operator communication; missing state remains visible |
| Factual terminal session outcome | Engine | Combines handoff with terminal control/git, optional eval, and cross-attempt delivery facts |
| Factual child outcome | Engine | Links/projects the terminal session outcome; makes no acceptance decision |
| Parent acceptance | Parent `planner`/acceptance role | Child recommendation is evidence only |

The current workflow-contract schema may need more than one undifferentiated
`accountable_roles` list to express “outer owns decomposition; inner may change
only the selected leaf's lifecycle.” This should remain an accountability and
review contract, not a path-level write fence.

## Correct goal granularity across layers

There is no universally correct number of files, tasks, WPs, PRs, or iterations
per child. A useful boundary is semantic:

> A child-session goal should describe a coherent, independently evaluable
> outcome that admits more than one reasonable leaf decomposition. If it
> already dictates the exact single edit or reconciliation step, it probably
> belongs in the child's plan as an inner leaf.

For Ultimate Memory, the current scopes look roughly like this:

| Layer | Better responsibility example |
| --- | --- |
| PM parent | Implement the complete design-backed Ultimate Memory program |
| Dispatched delivery session | Bring Phase 0 Foundations to an honestly development-ready state and satisfy its applicable exit criteria with merged evidence |
| Child outer plan | Reconcile convention authority, resolve remaining foundation gaps, deliver coherent PRs, verify CI, and close phase evidence |
| Child inner leaf | Reconcile the WP-0.1 stack-conventions authorities against merged scaffold evidence |
| Harness delegates | Inspect specific authorities, implement one bounded change, run checks, or review evidence for that leaf |

UGM already provides the right rough parent projection in its phase spine
(phases 0 through 8). Those phase outcomes—not individual WP rows—are the
natural initial PM work items. This should remain planner judgment rather than
an engine law: the planner may split a phase whose outcome is too broad, combine
coupled outcomes, or route around dependency gates, provided it records the
reason. The dispatcher transports the chosen phase/milestone outcome; the
child outer owns its WP/leaf/PR decomposition.

The example delivery goal still needs explicit observable completion criteria,
constraints, source references, and required evidence. “High-level” must not
mean vague. It means outcome-oriented rather than pre-decomposed into the one
edit the child outer should have selected itself.

A future three-layer topology uses the same rule recursively. The root can
dispatch a program milestone to a middle coordinator; that coordinator can
dispatch an integrated feature outcome to a delivery session; the delivery
outer can decompose it into inner leaves. No fixed agent graph or semantic
scheduler is required.

## Recommended upward flow

The intended parent/child exchange should be legible as this sequence:

1. Parent planner selects one milestone/outcome in its visible layer plan.
2. Dispatcher freezes that outcome contract and its exact input references; it
   does not narrow the outcome into a leaf.
3. Child outer creates the child's visible plan and selects its first leaf.
4. Child inner dynamically executes one leaf and returns evidence.
5. Child outer accepts, repairs, or re-scopes; it updates the plan, finished
   ledger, current state, and rolling semantic handoff.
6. Steps 3–5 repeat until outer judges that the child goal may be complete or
   that more independent evidence would be useful.
7. Using the frozen schedule context, outer chooses whether to run an eval now,
   leave the work for an imminent scheduled eval role, or decide without one.
8. Any scheduled eval role publishes an observation/receipt and yields the next
   semantic turn to outer; it never closes the session. Missing or malformed
   evidence is also returned as a diagnostic rather than retried until outer is
   starved.
9. Outer integrates the available evidence, publishes its semantic handoff,
   and alone decides whether to continue or write `goal_met` control.
10. At terminal completion, the engine writes the factual terminal session
    outcome. For a child, it also exposes that outcome to the parent.
11. Parent planner independently accepts, requests rework, or reroutes and then
    updates its own layer plan.

For a standalone one-layer run, steps 1–2 and 11 have no parent. The same outer
plan, inner leaves, optional eval evidence, completion decision, and semantic
handoff remain meaningful. Root status/inspection presents the engine's
terminal session outcome—semantic handoff plus control/git/delivery and any
eval facts—to the operator instead of an ancestor agent.

## Absolute-path and engine responsibilities

The Assignment should expose exact absolute paths for the canonical semantic
artifacts, not only the enclosing `project_state` directory. At minimum:

```text
layer_plan
layer_tasks
layer_current_state
layer_eval_state
layer_finished_ledger
layer_decisions
layer_handoff
session_state
workflow_roster
scheduler_view
```

These can be generated from exact entries in the frozen workflow contract. A
generic `contract_state_paths` map may scale better than adding one hard-coded
field for every future workflow artifact, provided the keys and paths are
stable and prompts name them unambiguously.

The current Assignment names the selected workflow and its current config
snapshot. The workflow contract lists role responsibilities, but it does not
tell the coordinator which roles are enabled, their priorities and cadence,
their `must_follow`/`run_after_successes` relationships, recent schedule state,
or which role would probably follow this attempt. A child Assignment also lacks
a named absolute path to that child's own `state.json`.

`workflow_roster` should therefore be a session-frozen full-set projection
containing every scheduled workflow role, responsibility, enabled state,
cadence, dependencies, and expected outputs. `scheduler_view` should be an
attempt-frozen projection containing recent factual history and an explicitly
conditional forecast such as:

```text
would_select_next_if_this_attempt_completes_mechanically_without_control_or_child_request
```

The forecast is context, not a promise: terminal control, child dispatch,
failure, stop/budget/max-turn/failure-cap conditions, or new durable state can
preempt it. It lets a smart coordinator avoid running an expensive eval itself
when an eval role is already due.

These are scheduled workflow roles, not all future “agents.” Loopy can know its
own roster. It cannot predict the dynamic harness delegates that the current
coordinator has not yet chosen, and it should not try.

The engine may:

- scaffold the files/directories;
- resolve and validate their absolute containment;
- freeze the workflow contract that declares role accountability;
- expose their paths in Assignments and status output;
- validate handoff identity/schema/hash structurally;
- link the handoff into the factual terminal session outcome and its child
  projection;
- validate the current orchestrator's control identity and any evidence refs it
  elects to cite; and
- surface absent/incomplete handoff, eval, and delivery lineage as factual
  completeness information without vetoing the orchestrator's semantic
  decision.

The engine should not:

- parse plan prose to select the next task;
- reject an iteration merely because plan prose is missing or semantically
  weak—the outer/eval/parent review path owns that repair;
- require an eval receipt, passing goal-check projection, or handoff as a
  protocol prerequisite for `goal_met`;
- turn an invalid advisory eval into terminal `goal_check_broken`;
- flip mechanical `HistoryEntry.success` or increment generic workflow-failure
  counters merely because advisory eval output is missing or invalid;
- veto a semantically surprising but valid decomposition;
- infer acceptance from task status text;
- turn the plan into a path-level ACL;
- run a parallel parent agent merely to keep its Markdown fresh; or
- replace the parent's independent acceptance with the child's recommendation.

This boundary preserves the core of D3, D4, D8, D10, and D12. D3 and D8 need
wording corrections described below, and D11's current mandatory-eval conclusion
must be replaced rather than cited as support.

## Follow-up: preserve cross-harness review without restoring a fixed graph

The historical prompts repeatedly used a useful causal pattern: one harness
performed research or implementation, then another harness reviewed the stable
artifact. Some versions also sent independent analysis to several harnesses in
parallel. This provided real diversity of tools and failure modes.

The part not worth restoring is the vendor-specific choreography. Hard-coding
“Codex implements, Claude reviews, Gemini reviews next” makes availability and
model churn part of every workflow prompt, requires review even for trivial
work, and prevents a coordinator from adapting the team to live evidence.

The replacement should expose two independent dimensions to every harness
coordinator:

1. a session-tree-frozen roster of all enabled harness families; and
2. each family's configured models/efforts through semantic strength tiers,
   using the stock vocabulary `frontier`, `strong`, `standard`, and `economy`.

Prompts can then state a judgment preference rather than a graph: parallelize
genuinely independent analyses; for consequential artifacts, prefer review by
a different enabled harness family than the primary author; use a frontier
tier when the confidence gain justifies the cost; and let the accountable
coordinator synthesize disagreements. Parallel delegates should write separate
findings, not race to edit one canonical file.

Eval-check creation deserves the strongest form of this guidance. Check design
shapes every later observation, and the historical eval-reviewer prompt still
assigned it to one hard-coded harness rather than using diversity. For a
non-trivial set, different families should independently analyze goal coverage
and failure modes in parallel, one integrator should draft the checks, and
different-family reviewers should attack the stable draft for gaps,
false-positive/negative paths, implementation coupling, gameability, ambiguity,
and evidence discoverability. The coordinator then publishes one coherent set.

This is an upgrade over both the old and current prompts. It must remain
guidance: no required family, agent count, all-provider quorum, review receipt,
or model-tier gate. If only one family is available or the work is trivial, the
coordinator proceeds autonomously with a proportionate review shape.

## State versus traces

The desired visibility belongs in compact semantic state:

- plan and selected work;
- current progress and blockers;
- decisions and alternatives;
- accepted work;
- eval inventory/headline and receipt links;
- semantic handoff; and
- factual child/parent acceptance records.

Detailed coordinator turns, dynamic spawn plans, full prompts, tool I/O, raw
eval output, and verbose reviews remain in gitignored traces under D12. If an
attempt-local thought changes what future attempts should do, the responsible
role promotes the resulting decision or plan change into compact state. The
system should not copy entire harness transcripts into the session plan merely
to make reasoning observable.

## Decision-log implications

This direction conflicts with current binding text and therefore cannot be
implemented as a quiet prompt edit.

### D3: retain the mechanical-success boundary; change the semantic arbiter

`IterationResult.success` should continue to mean that the harness assignment
ran without a Loopy/Team Harness execution error. It must not infer work quality
from worker process exits.

What changes is the next sentence in the architecture: the declared
orchestration role is the semantic arbiter and `control.json` is its stop
decision. Eval results are evidence available to that role; they are not the
only “real” semantic success mechanism.

### D4: retain check policy, make its scope conditional on evaluation being used

D4 can keep the stock LLM-as-judge policy and the distinction between
agent-authored checks and repository-owned suites. It governs how a stock eval
is authored and interpreted when an eval is run. It should not imply that every
session must run an eval or that an eval verdict owns terminal control.

UGM's prepared repository evals remain legitimate evidence. Its planner is
explicitly instructed to run them near the end because they are part of UGM's
completion method.

### D8: detection is evidence, not an unconditional semantic veto

D8's core remains right: do not build path fences, semantic scheduling vetoes,
or approval gates. Its current claim that a detected check failure necessarily
blocks acceptance is too strong under orchestrator-owned completion.

A failed or malformed eval should become visible evidence. The orchestrator
normally repairs the work or the check; it may also record that the observation
is stale, invalid, or irrelevant. The engine validates identities, schemas,
hashes, and topology, but it does not decide whether the semantic evidence is
sufficient. This preserves accountability without replacing model judgment
with an arbitrary programmatic rule.

### D10 and D12: retain their boundaries and extend the compact context

The durable-session versus dynamic-delegate distinction remains correct. So
does the state/evidence versus trace boundary. The schedule projection and eval
headline are compact state; raw evaluator prompts, transcripts, and reports
remain traces. D10 should additionally require the frozen workflow roster,
conditional scheduler view, absolute semantic-state paths, and enabled harness
capability roster. D12 should name those projections and the synthesized
cross-harness conclusions as compact state while leaving full parallel analyses
and reviewer transcripts in traces.

### D11: replace the conclusion while retaining its useful provenance rules

D11 should become approximately:

> Every session owns its scoped goal and names one orchestration role that owns
> its completion decision. Eval roles are optional evidence producers. A child
> result never proves its parent's broader goal complete. When eval evidence is
> produced or cited, its session/goal/check/judge/git provenance is validated,
> but successful terminal control does not require it.

For stock workflows, the completion owners are:

- `inner_outer_eval`: `outer`;
- `pm_planner_dispatcher`: `planner`.

Terminal-blocker producer identity and the D5 last-resort rules remain. A
spawned delegate still cannot publish durable control for its coordinator.

## Suggested implementation sequence after design agreement

This analysis is not itself a binding implementation decision. If the direction
is accepted, the smallest coherent follow-up is:

1. Amend D3/D8/D11 and the binding recursive-layer design: name the orchestrator
   as completion owner, make eval evidence optional, and add the universal
   layer-plan, topology-neutral standalone, and semantic-handoff invariants.
2. Update workflow contracts: `outer` owns completion in `inner_outer_eval`;
   restore `pm_planner_dispatcher` to planner plus dispatcher with planner as
   completion owner; keep inner eval roles as optional evidence producers.
3. Remove the v2 same-attempt eval-receipt/goal-check prerequisite for
   `goal_met`. Validate optional cited eval evidence, and turn invalid advisory
   eval output into non-failing diagnostics/events. Remove `emits_goal_check`
   from advisory runners or otherwise ensure bad advisory output neither flips
   mechanical history success nor increments generic failure counters that can
   starve outer and reach `workflow_failure_cap`.
4. Declare and scaffold canonical state paths, then expose those plus the
   current session state, frozen full workflow roster, and conditional scheduler
   view through every Assignment.
5. Restore the concise outer/inner planning lifecycle to the stock prompts,
   removing inner's authority to invent a durable broad plan and teaching outer
   how to use upcoming schedule context without duplicating evaluator work.
6. Add the engine-owned terminal session-outcome projection, bind handoff and
   delivery through accepted cross-attempt evidence, and preserve explicit
   completeness facts without making semantic artifacts terminal gates.
7. Add contract tests for standalone root `inner_outer_eval`, nested use,
   orchestrator completion with and without eval evidence, advisory eval
   failure, schedule awareness, terminal handoff, and independent parent
   acceptance.
8. Update Ultimate Memory's parent plan to phase-sized outcomes, make its
   delivery workflow topology-neutral, and instruct planner to run the prepared
   final eval inventory near program completion before making its own decision.
9. Improve status/session inspection so it displays the active stack and the
   absolute goal/plan/current-state/eval-state/handoff paths for every layer
   without interpreting their semantics.

## Final assessment

The recursive state machine is not fundamentally pointed in the wrong
direction. Its identity, path, evidence-provenance, and trace contracts are a
strong base. The regression is narrower and important: in making recursion
precise, the implementation stopped making each layer's semantic plan and
outcome equally precise, then made one optional evidence mechanism the mandatory
completion authority.

Recover the original outer/inner planning split, standardize visible state and
handoff paths at every depth, give the orchestrator full schedule context, make
evals advisory, and move parent dispatch goals up one abstraction level. Do that
without recovering the old prompt bloat or moving semantic judgment into the
engine. That produces a system in which agents remain autonomous, every layer
can be reviewed cold, a child can communicate upward without transcript
archaeology, and `inner_outer_eval` remains a complete one-layer system in its
own right.
